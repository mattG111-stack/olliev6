"""New listings, daily, from the portals — before the weekly file reaches them.

The weekly file is a snapshot. A home listed on Tuesday appears in it the
following Monday, and for a deal-finding product that is the whole game: an
underpriced listing is under offer inside a week, so six days late is the
difference between seeing it and reading about it.

About a hundred new listings a day across Auckland, which is what makes this
affordable. The two sources, and why each is asked the way it is:

  realestate.co.nz  Its actor takes `publication_date=last_24_hours`, which
                    filters SERVER-SIDE — so a daily run pays for the hundred
                    new listings and not the twelve thousand standing ones.
                    About $3 a month. `get_valuations` is what turns on the
                    council CV; it defaults to false and is easy to forget.

  oneroof           No date filter, so it is asked for the newest page of
                    listings and capped. It earns its place by carrying the
                    RATING VALUATION and the land/improvement split — the two
                    numbers the land-only-CV rule needs, which realestate does
                    not publish. About $43 a month at a 300-row daily cap.

  trademe           Not swept. Its actor returns no valuation of any kind, so a
                    listing from it would arrive with no CV, and a CV is what
                    everything downstream is anchored to.

Nothing goes live by itself. Each new listing lands as a PortalListing for a
person to approve, because this is a claim scraped off someone else's page and
the moment it becomes a row in the live batch it is indistinguishable from data
we stand behind.

WHAT NO PORTAL CARRIES: `zoning` and `type_of_title`. Those are council and LINZ
records rather than listing data, and both gate the subdivision engine. An
approved row prices normally and reads as "not subdividable" until the weekly
file catches up — a known gap, not a verdict about the property.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import BatchType, ImportBatch, PortalListing, PropertyForSale
from ..trademe import address_key
from .apify import ApifyUnavailable, num, pick, run_actor
from .sources import ACTORS

log = logging.getLogger(__name__)

# Swept for new listings. Trade Me is absent on purpose — see the note above.
NEW_LISTING_SOURCES = ("realestate", "oneroof")

# How many rows a single daily sweep will accept from one portal. A hundred a
# day is the observed rate; three hundred is headroom for a busy Tuesday and a
# hard stop if a filter ever silently stops filtering and the actor starts
# returning the whole city.
DAILY_CAP = 300

# OneRoof has no date filter, so it is asked for its newest listings and the cap
# does the rest. This URL is the one thing here I could not verify — the
# container this was written in cannot reach oneroof.co.nz — so if the sweep
# comes back with old listings, this is what to look at first. A wrong URL costs
# accuracy of the window, never correctness: everything is deduped by address
# before it is stored, so the worst case is paying for rows we already have.
ONEROOF_NEWEST_URL = (
    "https://www.oneroof.co.nz/search/houses-for-sale/region_auckland-35"
    "?sort=newest"
)


# Sold, weekly. Sales do not move the way listings do — a week-old sale is
# still a comp, where a week-old listing is often already under offer — so this
# runs on Sunday rather than nightly and costs about $13 a month for both
# portals at Auckland's ~50-80 sales a day.
#
# Neither portal can filter sold records by date server-side (realestate's
# publication_date applies to ACTIVE listings only), so both are asked
# newest-first and capped. The cap is the only thing between a weekly run and
# every sale ever recorded, so it is generous but real.
WEEKLY_SOLD_CAP = 1000
SOLD_SOURCES = ("realestate", "oneroof")

ONEROOF_SOLD_URL = (
    "https://www.oneroof.co.nz/search/sold/region_auckland-35?sort=newest"
)


def _sold_payload(source: str, *, cap: int = WEEKLY_SOLD_CAP) -> dict:
    if source == "realestate":
        return {
            "listing_type": "sold",
            "city": ["Auckland"],
            # The actor's own ordering for sold-history work.
            "sort_by": "latest-sale",
            "get_valuations": True,
            "limit": cap,
        }
    if source == "oneroof":
        return {
            "mode": "sold",                       # a value in the actor's enum
            "region": "auckland-35",
            "startUrls": [ONEROOF_SOLD_URL],
            "includeDetails": True,
            "maxResults": cap,
        }
    raise ValueError(f"no sold sweep for {source!r}")


def _payload(source: str, *, hours: int = 24, cap: int = DAILY_CAP) -> dict:
    if source == "realestate":
        return {
            "listing_type": "sale",
            "city": ["Auckland"],
            # The whole economy of this feature. Server-side, so we are charged
            # for new listings only.
            "publication_date": ("last_24_hours" if hours <= 24
                                 else "last_3_days" if hours <= 72
                                 else "last_7_days"),
            "sort_by": "latest",
            # Its CV and estimate exist ONLY when this is on, and it defaults
            # to false.
            "get_valuations": True,
            "limit": cap,
        }
    if source == "oneroof":
        return {
            "mode": "houses-for-sale",
            "region": "auckland-35",
            "startUrls": [ONEROOF_NEWEST_URL],
            # What carries ratingValuation and the land/improvement split.
            "includeDetails": True,
            "maxResults": cap,
        }
    raise ValueError(f"no new-listing sweep for {source!r}")


# ---------------------------------------------------------------------------
# Normalising two very different shapes into one row
# ---------------------------------------------------------------------------
def _first_url(v):
    if isinstance(v, (list, tuple)):
        for x in v:
            if isinstance(x, str) and x.strip():
                return x
            if isinstance(x, dict) and (x.get("url") or x.get("base_url")):
                return x.get("url") or x.get("base_url")
        return None
    return v if isinstance(v, str) and v.strip() else None


def _from_oneroof(item: dict) -> dict:
    """Field names checked against the actor's published output schema.

    Its money and areas are STRINGS ("$1,250,000", "182m²") — num() is what
    turns those into numbers, and it is the reason num() had to stop deleting
    non-digits and start parsing.
    """
    return dict(
        source="oneroof",
        source_id=pick(item, "id", "listingId"),
        url=pick(item, "url"),
        address=pick(item, "address"),
        suburb=pick(item, "suburb"),
        district=pick(item, "locality", "region"),
        property_type=pick(item, "propertyType"),
        price_numeric=num(pick(item, "priceValue")),
        price_display=pick(item, "priceText", "priceMethod"),
        cv_numeric=num(pick(item, "ratingValuation")),
        land_value_numeric=num(pick(item, "landValue")),
        improvement_value_numeric=num(pick(item, "improvementValue")),
        floor_area_m2=num(pick(item, "floorArea")),
        land_area_m2=num(pick(item, "landArea")),
        beds=num(pick(item, "bedrooms")),
        baths=num(pick(item, "bathrooms")),
        carspaces=num(pick(item, "parking")),
        latitude=num(pick(item, "latitude")),
        longitude=num(pick(item, "longitude")),
        image_url=_first_url(item.get("images")),
        listed_date=pick(item, "listedDate"),
        description=pick(item, "description"),
    )


def _from_realestate(item: dict) -> dict:
    """Everything useful is nested: property.*, location.*, entity.*, media.*."""
    prop = item.get("property") if isinstance(item.get("property"), dict) else {}
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    ent = item.get("entity") if isinstance(item.get("entity"), dict) else {}
    media = item.get("media") if isinstance(item.get("media"), dict) else {}
    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
    val = item.get("valuation") if isinstance(item.get("valuation"), dict) else {}
    listing = item.get("listing") if isinstance(item.get("listing"), dict) else {}

    return dict(
        source="realestate",
        source_id=pick(listing, "listing_id") or pick(item, "record_id"),
        url=ent.get("url") or pick(item, "listing_url"),
        address=loc.get("full_address") or loc.get("address"),
        suburb=loc.get("suburb") or loc.get("publish_suburb"),
        district=loc.get("district") or loc.get("region"),
        property_type=prop.get("property_type"),
        # No numeric price on this actor — only the text the vendor published.
        # An asking price we cannot read is better than one we invented, and
        # the pipeline already knows what to do with a price it cannot parse.
        price_numeric=num(pick(item, "price", "sale_price")),
        price_display=pricing.get("price_text"),
        cv_numeric=num(pick(val, "capital_value", "council_value", "cv")),
        land_value_numeric=num(pick(val, "land_value")),
        improvement_value_numeric=num(pick(val, "improvement_value")),
        floor_area_m2=num(prop.get("floor_area")),
        land_area_m2=num(prop.get("land_area")),
        beds=num(prop.get("bedrooms")),
        baths=num(prop.get("bathrooms")),
        carspaces=num(prop.get("garages") or prop.get("carports")),
        latitude=num(loc.get("latitude")),
        longitude=num(loc.get("longitude")),
        image_url=_first_url(media.get("photos") or []),
        listed_date=listing.get("published_at") or listing.get("created_at"),
        description=ent.get("description"),
    )


_READERS = {"oneroof": _from_oneroof, "realestate": _from_realestate}


def _sold_extras(source: str, item: dict) -> dict:
    """The three things a sale has that a listing does not.

    OneRoof publishes `soldPrice` and `soldDate` alongside everything else, so a
    sold record is a normal row plus these. realestate carries the sale on the
    listing itself once its status is sold.
    """
    if source == "oneroof":
        return dict(
            sale_price=num(pick(item, "soldPrice", "lastSalePrice")),
            sold_date=pick(item, "soldDate", "lastSaleDate"),
            sale_method=pick(item, "priceMethod"),
            days_on_market=num(pick(item, "daysOnMarket")),
        )
    listing = item.get("listing") if isinstance(item.get("listing"), dict) else {}
    return dict(
        sale_price=num(pick(item, "sale_price", "sold_price", "price")),
        sold_date=pick(listing, "sold_date", "sale_date") or pick(item, "sold_date"),
        sale_method=pick(listing, "listing_type", "price_code"),
        days_on_market=num(pick(listing, "days_on_market")),
    )


def to_listing(source: str, item: dict, *, kind: str = "for_sale") -> dict | None:
    """One portal item as a PortalListing row, or None if it is not usable.

    An address is the minimum. Without one there is no way to tell whether we
    already hold the property, and a listing we cannot deduplicate would arrive
    again every single day.
    """
    reader = _READERS.get(source)
    if reader is None or not isinstance(item, dict):
        return None
    row = reader(item)
    if not row.get("address"):
        return None
    row["address_key"] = address_key(row["address"], row.get("suburb"))
    if not row["address_key"]:
        return None
    row["kind"] = kind
    if kind == "sold":
        row.update(_sold_extras(source, item))
        # A sale with no price or no date is not a comp. It cannot be placed in
        # time or compared to anything, and a sold record that is neither is
        # just an address.
        if not row.get("sale_price") or not row.get("sold_date"):
            return None
    row["raw_json"] = json.dumps(item)[:200_000]
    return row


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
def fetch(source: str, *, hours: int = 24, cap: int = DAILY_CAP,
          kind: str = "for_sale", runner=None, db=None) -> list[dict]:
    """Ask one portal what it published recently. Never raises.

    `runner` exists so the tests can drive the whole sweep without a network
    and without a token.
    """
    # The session is threaded through so a token typed into the admin panel
    # runs, not only one set in the environment. The test doubles take three
    # arguments, so the session is closed over rather than added to the call.
    call = runner or (lambda a, pl, limit=None: run_actor(a, pl, limit=limit, db=db))
    payload = (_sold_payload(source, cap=cap) if kind == "sold"
               else _payload(source, hours=hours, cap=cap))
    try:
        items = call(ACTORS[source], payload, limit=cap)
    except ApifyUnavailable as e:
        log.info("%s new-listing sweep unavailable: %s", source, e)
        return []
    except Exception as e:                        # noqa: BLE001
        log.warning("%s new-listing sweep failed: %s", source, e)
        return []
    out = []
    for item in items or []:
        row = to_listing(source, item, kind=kind)
        if row:
            out.append(row)
    return out


# A sale this far from what the suburb has been doing is worth reading before
# it joins the comp pool. Not a rejection — a genuinely exceptional sale happens,
# and throwing those away is how a model learns a market that does not exist —
# but the difference between $1.2M and $12M is a digit, and a digit poisons the
# suburb's $/m2 rate and its sale/CV ratio for everyone.
SALE_SANE_LOW, SALE_SANE_HIGH = 0.25, 4.0
SALE_MIN_COMPS = 5


def _price_flag(db: Session, row: dict) -> str | None:
    """Does this sale price sit anywhere near what this suburb sells for?

    Measured against the suburb's own recorded sales, not a global rule: $400k
    is unremarkable in Papakura and impossible in Herne Bay. Silent when the
    suburb has too few sales to have a normal — an opinion from four sales is
    not worth flagging a fifth over.
    """
    from ..ingest import sold_batch_ids
    from ..models import PropertySold

    price = row.get("sale_price")
    suburb = (row.get("suburb") or "").strip()
    if not price or not suburb:
        return None
    batches = sold_batch_ids(db, "Auckland")
    if not batches:
        return None
    prices = [p for (p,) in db.query(PropertySold.sale_price)
              .filter(PropertySold.import_batch_id.in_(batches),
                      PropertySold.suburb == suburb,
                      PropertySold.sale_price.isnot(None),
                      PropertySold.sale_price > 0).all()]
    if len(prices) < SALE_MIN_COMPS:
        return None
    prices.sort()
    median = prices[len(prices) // 2]
    if not median:
        return None
    ratio = price / median
    if ratio < SALE_SANE_LOW:
        return f"{ratio:.0%} of the {suburb} median — check for a missing digit"
    if ratio > SALE_SANE_HIGH:
        return f"{ratio:.0%} of the {suburb} median — check for an extra digit"
    return None


def _live_keys(db: Session) -> set[str]:
    """Every address already in the live for-sale batch.

    Read once per sweep rather than queried per row: a hundred lookups against
    a ten-thousand-row table is the kind of thing that is fine until the day it
    is not.
    """
    batch = (db.query(ImportBatch.id)
             .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                     ImportBatch.is_active.is_(True))
             .order_by(ImportBatch.id.desc()).first())
    if not batch:
        return set()
    rows = (db.query(PropertyForSale.address, PropertyForSale.suburb)
            .filter(PropertyForSale.import_batch_id == batch[0]).all())
    return {k for k in (address_key(a, s) for a, s in rows) if k}


def record(db: Session, rows: list[dict]) -> tuple[int, int]:
    """Store what the sweep found. Returns (new, skipped).

    Skipped means one of two things, and both are the point: we already have the
    property in the live batch, or this portal already offered it and it is
    sitting in the pending list — or was refused. A daily sweep of a listing
    that stays on the market for six weeks must produce one row, not forty-two.
    """
    if not rows:
        return 0, 0
    kind = rows[0].get("kind", "for_sale")
    # A sale is not compared against the live FOR-SALE batch — the same house
    # can be listed and then sold, and both are true. Sales are deduped against
    # the sold records we already hold.
    have = _live_keys(db) if kind == "for_sale" else _sold_keys(db)
    # Built with the SAME key the rows below are tested with: a listing is one
    # per address, a sale one per address per month. Keying these two
    # differently is how the sweep recorded the same sale three times.
    seen = {
        (r.source, r.kind,
         _sale_key({"address_key": r.address_key, "sold_date": r.sold_date})
         if r.kind == "sold" else r.address_key)
        for r in db.query(PortalListing.source, PortalListing.kind,
                          PortalListing.address_key,
                          PortalListing.sold_date).all()
    }

    new = skipped = 0
    for row in rows:
        # A listing is one per address; a SALE is one per address per month,
        # because a house genuinely sells more than once and a 2019 sale must
        # not stop us recording its 2026 one.
        key = (_sale_key(row) if row["kind"] == "sold" else row["address_key"])
        if key in have or (row["source"], row["kind"], key) in seen:
            skipped += 1
            continue
        if row["kind"] == "sold":
            row["price_flag"] = _price_flag(db, row)
        db.add(PortalListing(**row, status="pending"))
        seen.add((row["source"], row["kind"], key))
        new += 1
    db.commit()
    return new, skipped


def _sale_key(row: dict) -> str:
    """address + the month it sold in — see _sold_keys."""
    from ..periods import sold_year_month

    ym = sold_year_month(row.get("sold_date"))
    stamp = f"{ym[0]:04d}-{ym[1]:02d}" if ym else "?"
    return f"{row['address_key']}|{stamp}"


def _sold_keys(db: Session) -> set[str]:
    """Every sale we already hold, by address AND month.

    Address alone would be wrong: a house genuinely sells more than once, and a
    2019 sale must not stop us recording its 2026 one. The month is what makes
    the same sale from two portals a duplicate and two real sales two records.
    """
    from ..ingest import sold_batch_ids
    from ..models import PropertySold
    from ..periods import sold_year_month

    batches = sold_batch_ids(db, "Auckland")
    if not batches:
        return set()
    rows = (db.query(PropertySold.address, PropertySold.suburb,
                     PropertySold.sold_date)
            .filter(PropertySold.import_batch_id.in_(batches)).all())
    out = set()
    for address, suburb, sold in rows:
        key = address_key(address, suburb)
        ym = sold_year_month(sold)
        if key and ym:
            out.add(f"{key}|{ym[0]:04d}-{ym[1]:02d}")
    return out


def sweep(db: Session, *, sources=NEW_LISTING_SOURCES, hours: int = 24,
          cap: int = DAILY_CAP, kind: str = "for_sale", runner=None) -> dict:
    """One pass over every portal. Returns a per-source summary."""
    out: dict[str, dict] = {}
    for source in sources:
        rows = fetch(source, hours=hours, cap=cap, kind=kind, runner=runner,
                     db=db)
        new, skipped = record(db, rows)
        out[source] = {"found": len(rows), "new": new, "skipped": skipped}
        log.info("%s %s: %d found, %d new, %d already known",
                 source, kind, len(rows), new, skipped)
    return out


def sweep_sold(db: Session, *, sources=SOLD_SOURCES, cap: int = WEEKLY_SOLD_CAP,
               runner=None) -> dict:
    """The weekly pass for sales.

    Weekly rather than daily because a week-old sale is still a comp, where a
    week-old listing is often already under offer. That cadence is also what
    makes it cheap: ~$13 a month for both portals at Auckland's 50-80 sales a
    day, against the ~$90 a daily run would cost for the same records.
    """
    return sweep(db, sources=sources, cap=cap, kind="sold", runner=runner)


# ---------------------------------------------------------------------------
# Approving one
# ---------------------------------------------------------------------------
# What carries across to a PropertyForSale. Deliberately only facts about the
# property — no valuation, no score, no flag. Everything derived is derived by
# the pricing engine from these, through exactly the same rules as the weekly
# file, so a portal-sourced listing cannot take a shortcut a scraped one cannot.
_CARRIED = (
    "address", "suburb", "district", "property_type", "price_numeric",
    "price_display", "cv_numeric", "land_value_numeric",
    "improvement_value_numeric", "floor_area_m2", "land_area_m2",
    "latitude", "longitude", "image_url",
)


# What a sale carries into properties_sold. Same principle as _CARRIED: facts
# only, and every number the comp engine derives it derives itself.
_CARRIED_SOLD = (
    "address", "suburb", "district", "property_type", "cv_numeric",
    "land_value_numeric", "improvement_value_numeric", "floor_area_m2",
    "land_area_m2", "latitude", "longitude", "sale_price", "sold_date",
    "sale_method", "days_on_market",
)

# Sales from the portals accumulate in their own delivery, kept apart from the
# uploaded files. A sold batch is a DELIVERY, not the dataset — every reader
# already goes through sold_batch_ids — so this joins the pool rather than
# replacing anything, and its filename says where it came from.
PORTAL_SOLD_FILENAME = "portal-sweep"


def _portal_sold_batch(db: Session):
    """The batch portal-found sales land in, created on first use."""
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.SOLD.value,
                 ImportBatch.filename == PORTAL_SOLD_FILENAME,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    if b is None:
        b = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                        filename=PORTAL_SOLD_FILENAME, is_active=True,
                        status="published")
        db.add(b)
        db.flush()
    return b


def _approve_sale(db: Session, row, user_id: int | None) -> tuple[bool, str]:
    """Put one portal-found sale into the sold pool.

    No pricing call: a sale is not valued, it is what everything else is valued
    AGAINST. It joins the comp pool and the suburb's sale/CV ratio from the next
    read onward.
    """
    from ..models import PropertySold

    batch = _portal_sold_batch(db)
    sale = PropertySold(import_batch_id=batch.id)
    for field in _CARRIED_SOLD:
        setattr(sale, field, getattr(row, field))
    sale.beds = int(row.beds) if row.beds else None
    sale.baths = int(row.baths) if row.baths else None
    db.add(sale)
    db.flush()

    row.status = "approved"
    row.property_id = sale.id
    row.decided_at = datetime.now(timezone.utc)
    row.decided_by_id = user_id
    db.commit()
    return True, ""


def approve(db: Session, listing_id: int, *, user_id: int | None = None,
            reprice=None) -> tuple[bool, str]:
    """Put one portal listing into the live batch and price it.

    Returns (added, why-not). Priced through reprice_one, which is the same path
    the weekly file and the portal fills use — including the hold rules, so a
    listing that arrives without enough to value it is held rather than shown.
    """
    from ..reprice import reprice_one

    row = db.get(PortalListing, listing_id)
    if row is None:
        return False, "no such listing"
    if row.status != "pending":
        return False, f"already {row.status}"
    if row.kind == "sold":
        return _approve_sale(db, row, user_id)

    batch = (db.query(ImportBatch)
             .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                     ImportBatch.is_active.is_(True))
             .order_by(ImportBatch.id.desc()).first())
    if batch is None:
        return False, "no live batch to add it to"

    # It may have arrived in the weekly file between the sweep and the decision,
    # which is the good outcome — the council data comes with it.
    if row.address_key in _live_keys(db):
        row.status = "superseded"
        row.decided_at = datetime.now(timezone.utc)
        row.decided_by_id = user_id
        db.commit()
        return False, "the weekly file already has this property"

    prop = PropertyForSale(import_batch_id=batch.id)
    for field in _CARRIED:
        setattr(prop, field, getattr(row, field))
    prop.beds = int(row.beds) if row.beds else None
    prop.baths = int(row.baths) if row.baths else None
    prop.asking_price = row.price_numeric
    db.add(prop)
    db.flush()

    row.status = "approved"
    row.property_id = prop.id
    row.decided_at = datetime.now(timezone.utc)
    row.decided_by_id = user_id
    db.commit()

    try:
        (reprice or reprice_one)(db, prop.id)
    except ValueError:
        pass                                      # nothing to price against yet
    except Exception as e:                        # noqa: BLE001
        log.info("pricing a new portal listing failed for %s: %s", prop.id, e)
    return True, ""


def reject(db: Session, listing_id: int, *, user_id: int | None = None) -> bool:
    """Refuse one, and keep it. Tomorrow's sweep finds the same listing on the
    same portal, and the record of the decision is what stops it coming back
    looking new."""
    row = db.get(PortalListing, listing_id)
    if row is None or row.status != "pending":
        return False
    row.status = "rejected"
    row.decided_at = datetime.now(timezone.utc)
    row.decided_by_id = user_id
    db.commit()
    return True


def pending(db: Session, *, kind: str | None = None,
            limit: int = 200) -> list[PortalListing]:
    q = db.query(PortalListing).filter(PortalListing.status == "pending")
    if kind:
        q = q.filter(PortalListing.kind == kind)
    return (q.order_by(PortalListing.created_at.desc(), PortalListing.id.desc())
            .limit(limit).all())
