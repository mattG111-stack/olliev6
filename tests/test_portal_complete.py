"""Filling in what the portal didn't carry.

    "where is the rest of the info for these houses ?"
    "and how do we load it too our systems and run the pricing"

The second half already worked: approving a listing puts it in the live batch
and prices it through the same path as the weekly file, hold rules included.

The first half did not. A portal advertises a listing; it does not publish a
council record. So a scraped row arrives with an address, a price and a floor
area and NO council valuation — and a listing with no CV cannot be valued,
because every method here is anchored on it. The only chance to fill that gap
came after approval, once the row was already live and only if it happened to
land in the enrich work list. The decision was being made on the thinnest
version of the row, and a listing marked "no council record" was approved
straight into a hold.

Two separate faults, one of them silent for as long as the feature has existed:

  CARSPACES WAS DROPPED ON APPROVAL. Scraped, stored on the portal row, in
  neither _CARRIED nor the explicit beds/baths lines. Every portal listing that
  reached the live batch priced as though it had no parking — beta-7 of the
  hedonic, a real number we already held, thrown away on the way in.

  NOTHING FILLED THE GAPS FIRST. Which is what this module is.
"""
from __future__ import annotations

import pytest

from app.models import (BatchType, ImportBatch, PortalListing, PropertyForSale)
from app.portals.complete import _FILLABLE, fill_pending, needs_filling
from app.propertyvalue import PV_ERROR, PV_NOT_FOUND, PV_OK


def _pending(db, **kw):
    d = dict(source="oneroof", kind="for_sale", status="pending",
             address="24 Lomandra Street", address_key="24 lomandra street",
             suburb="Westgate", property_type="House",
             price_numeric=900_000.0, floor_area_m2=149.0)
    d.update(kw)
    row = PortalListing(**d)
    db.add(row)
    db.commit()
    return row


def _answer(monkeypatch, rec, status=PV_OK):
    import app.portals.complete as C
    monkeypatch.setattr(C, "pv_lookup_status", lambda q, *a, **k: (rec, status))


# ---- the gap this closes ----------------------------------------------------
def test_a_listing_with_no_council_record_gets_one(db_session, monkeypatch):
    """The row marked "no council record" on the screen. Without a CV it cannot
    be valued at all, so approving it was approving it into a hold."""
    row = _pending(db_session)
    assert row.cv_numeric is None
    _answer(monkeypatch, {"cv": 1_050_000.0, "land_value": 600_000.0,
                          "improvement_value": 450_000.0, "land_area_m2": 405.0})

    out = fill_pending(db_session)

    db_session.refresh(row)
    assert row.cv_numeric == 1_050_000.0
    assert out["council_records_found"] == 1


def test_what_the_portal_published_is_never_overwritten(db_session, monkeypatch):
    """The portal is looking at the actual listing. The lookup is matching on an
    address string and can match the wrong house. Where they disagree, the
    listing wins."""
    row = _pending(db_session, floor_area_m2=149.0, cv_numeric=1_000_000.0)
    _answer(monkeypatch, {"cv": 9_999_999.0, "floor_area_m2": 55.0})

    fill_pending(db_session)

    db_session.refresh(row)
    assert row.floor_area_m2 == 149.0
    assert row.cv_numeric == 1_000_000.0


def test_the_asking_price_is_never_touched(db_session, monkeypatch):
    """The one fact a portal IS authoritative about, and a council record has no
    opinion on it."""
    row = _pending(db_session, price_numeric=900_000.0)
    _answer(monkeypatch, {"cv": 1_050_000.0, "price_numeric": 1})

    fill_pending(db_session)

    db_session.refresh(row)
    assert row.price_numeric == 900_000.0


def test_filling_approves_nothing(db_session, monkeypatch):
    """Filling is what makes the decision possible, not the decision."""
    row = _pending(db_session)
    _answer(monkeypatch, {"cv": 1_050_000.0})

    fill_pending(db_session)

    db_session.refresh(row)
    assert row.status == "pending"
    assert db_session.query(PropertyForSale).count() == 0


def test_a_row_that_already_has_everything_is_not_asked_about(db_session,
                                                              monkeypatch):
    """A lookup that buys nothing still costs a request, and a weekly load of
    these adds up."""
    row = _pending(db_session, cv_numeric=1_000_000.0, land_value_numeric=6e5,
                   improvement_value_numeric=4e5, floor_area_m2=149.0,
                   land_area_m2=405.0, beds=4, baths=2, carspaces=2,
                   prior_sale_price=880_000.0, prior_sale_date="2019-03-04")
    assert not needs_filling(row)

    _answer(monkeypatch, {"cv": 1.0})
    assert fill_pending(db_session)["looked_up"] == 0


def test_an_address_the_council_does_not_hold_is_reported_not_hidden(
        db_session, monkeypatch):
    row = _pending(db_session)
    _answer(monkeypatch, None, PV_NOT_FOUND)

    out = fill_pending(db_session)

    assert out["not_found"] == 1
    assert out["council_records_found"] == 0
    db_session.refresh(row)
    assert row.cv_numeric is None


def test_an_unreachable_lookup_is_counted_apart_from_a_miss(db_session,
                                                            monkeypatch):
    """Same distinction the enrich stage now makes: one is a data limit, the
    other is an outage, and they need opposite responses."""
    _pending(db_session)
    _answer(monkeypatch, None, PV_ERROR)

    out = fill_pending(db_session)
    assert out["unreachable"] == 1 and out["not_found"] == 0


def test_the_keys_it_reads_are_the_keys_the_lookup_returns(db_session):
    """A misspelled key fills nothing and says nothing — the quietest kind of
    failure. car_spaces was exactly that trap: theirs has the underscore, ours
    does not."""
    import inspect
    import re

    from app.propertyvalue import _shape

    keys = set(re.findall(r'"([a-z_0-9]+)":', inspect.getsource(_shape)))
    assert [t for _, t in _FILLABLE if t not in keys] == []


# ---- the field that was being dropped ---------------------------------------
def test_approving_a_listing_carries_its_parking(db_session):
    """Scraped, stored, and then dropped: carspaces was in neither _CARRIED nor
    the explicit beds/baths lines, so every approved portal listing priced as
    though it had no parking."""
    from app.portals.listings import approve

    batch = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                        filename="live.csv", is_active=True, status="published")
    db_session.add(batch)
    db_session.commit()

    row = _pending(db_session, beds=4.0, baths=2.0, carspaces=2.0,
                   cv_numeric=1_050_000.0, land_area_m2=405.0)
    approve(db_session, row.id, reprice=lambda *a, **k: None)

    prop = db_session.query(PropertyForSale).one()
    assert prop.cars == 2, "the parking we already had was dropped on the way in"
    assert prop.beds == 4 and prop.baths == 2


# ---- everything else that was being dropped ---------------------------------
#
# Checked by comparing the two models field by field rather than by reading the
# carried list, because reading the list is how four fields stayed missing.
def _approved(db, **kw):
    from app.portals.listings import approve

    batch = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                        filename="live.csv", is_active=True, status="published")
    db.add(batch)
    db.commit()
    row = _pending(db, **kw)
    approve(db, row.id, reprice=lambda *a, **k: None)
    return db.query(PropertyForSale).one()


def test_the_link_to_the_listing_comes_with_it(db_session):
    """An approved portal row was a property with no way to go and look at it —
    on a row whose whole point is that it is too new to be anywhere else."""
    prop = _approved(db_session, url="https://www.oneroof.co.nz/property/12345")
    assert prop.url == "https://www.oneroof.co.nz/property/12345"


def test_the_description_comes_with_it(db_session):
    """detect_pool() reads it, and a description is the only place a portal says
    anything about condition. Dropped, and the pool went with it."""
    prop = _approved(db_session, description="Sunny 4 bedroom with a pool.")
    assert prop.description and "pool" in prop.description


def test_the_portals_own_estimate_lands_in_that_portals_column(db_session):
    """Captured on the scraped row and then dropped — so a listing OneRoof
    valued at $1.2M arrived with nothing saying so, while the same figure
    fetched later by an enrich run was kept. Same fact, two paths, one of them
    throwing it away."""
    prop = _approved(db_session, source="oneroof", estimate=1_200_000.0,
                     estimate_low=1_100_000.0, estimate_high=1_300_000.0,
                     url="https://www.oneroof.co.nz/property/1")

    assert prop.oneroof_valuation == 1_200_000.0
    assert prop.oneroof_valuation_low == 1_100_000.0
    assert prop.oneroof_valuation_high == 1_300_000.0
    assert prop.oneroof_url == "https://www.oneroof.co.nz/property/1"


def test_an_estimate_never_lands_in_another_portals_column(db_session):
    """Each portal's number is theirs. Homes' estimate in OneRoof's column is
    how a column stops meaning what its name says."""
    prop = _approved(db_session, source="homes", estimate=900_000.0)
    assert prop.homes_valuation == 900_000.0
    assert prop.oneroof_valuation is None


def test_how_old_the_listing_is_comes_with_it(db_session):
    """The 90-day stale rule reads days_on_market, and a portal for-sale scrape
    never carries it — so a portal listing was the only kind in the system that
    rule could not see, however old it was. The panel is titled "listed in the
    last day" and has surfaced rows dated 12-12-2025."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=257)).strftime("%Y-%m-%d")
    prop = _approved(db_session, listed_date=old)

    assert prop.days_on_market is not None
    assert 250 <= prop.days_on_market <= 264


def test_an_unreadable_date_leaves_the_age_unknown(db_session):
    """None means unknown. ZERO would mean "listed today", which is how an
    eighteen-month-old listing gets sold as fresh."""
    from app.portals.listings import _days_since

    assert _days_since("rubbish") is None
    assert _days_since("") is None
    assert _days_since("2099-01-01") is None, "a future date is a bad parse"


def test_no_field_that_exists_on_both_sides_is_silently_dropped(db_session):
    """The check that found the other four. Reading the carried list is how they
    stayed missing; comparing the two models is how they were found."""
    from sqlalchemy import inspect as sq

    from app.portals.listings import _CARRIED

    listing = {c.key for c in sq(PortalListing).mapper.column_attrs}
    forsale = {c.key for c in sq(PropertyForSale).mapper.column_attrs}
    # Set explicitly in approve() rather than carried by name.
    explicit = {"beds", "baths", "cars", "asking_price", "import_batch_id",
                "days_on_market"}
    # The row's own identity, which belongs to the portal record, not the listing.
    its_own = {"id", "created_at"}
    renamed = {"price_numeric": "asking_price", "carspaces": "cars"}

    carried = set(_CARRIED) | explicit
    dropped = sorted(f for f in listing
                     if f not in its_own
                     and renamed.get(f, f) in forsale
                     and f not in carried and renamed.get(f, f) not in carried)
    assert dropped == [], f"scraped, stored, and then thrown away: {dropped}"


# ---- parity with the weekly file --------------------------------------------
#
#   "it needs to match the data we already get other wise its a waste of time"
#
# The right standard, and full parity is not reachable: a weekly-file listing
# carries 51 fields a portal row does not, and around thirty of them are the
# agent's phone number, the school zones, the sale history and the price trends.
# No portal publishes those and no council record has them.
#
# But only TWO of the 51 reach the pricing engine, and those are the ones worth
# arguing about. Everything else in that gap is real data that changes no
# valuation. These two now arrive.
def test_the_last_sale_reaches_the_priced_listing(db_session, monkeypatch):
    """Read by the pipeline twice: it is how a scraped "price" that is really
    the last sale gets caught, and it is read again when a home is relisted near
    what it fetched last time."""
    _answer(monkeypatch, {"cv": 1_050_000.0, "last_sale_price": 880_000.0,
                          "last_sale_date": "2019-03-04"})
    row = _pending(db_session)
    fill_pending(db_session)
    db_session.refresh(row)
    assert row.prior_sale_price == 880_000.0

    prop = _approved_existing(db_session, row)
    assert prop.valuation_last_sold_value == 880_000.0
    assert prop.valuation_last_sold_date == "2019-03-04"


def test_a_pool_in_the_description_reaches_the_priced_listing(db_session):
    """The other one. A pool changes the comp set a property is valued against,
    and the description is the only place a portal mentions it — which is why
    carrying the description mattered rather than being tidiness."""
    prop = _approved(db_session,
                     description="Sunny four bedroom with a heated swimming pool.")
    assert prop.has_swimming_pool is True


def test_a_description_with_no_pool_does_not_invent_one(db_session):
    prop = _approved(db_session, description="Sunny four bedroom, double garage.")
    assert not prop.has_swimming_pool


def test_the_two_pricing_fields_are_the_only_ones_that_matter(db_session):
    """Pins the claim, by working it out rather than by listing it.

    An earlier version of this test named ten weekly-file fields by hand and
    checked they were absent from pipeline.run. That pins ten fields, not the
    claim — the claim is about all of them, and about every pricing module, not
    just the one function. So: derive the gap from the models, derive what
    pricing reads from the source, and intersect.

    If a field a portal row does not carry starts being read anywhere in
    app/pricing, this fails by name and the gap gets looked at again instead of
    assumed closed.
    """
    import glob
    import inspect
    import re

    from app.models import PropertyForSale
    from app.portals import listings

    # The gap: every column on a priced listing that approval does not populate,
    # either directly or through one of the carried-field tables.
    approve_src = inspect.getsource(listings.approve)
    carried = set()
    for name in dir(listings):
        if name.startswith("_CARRIED"):
            v = getattr(listings, name)
            if isinstance(v, (tuple, list, set)):
                carried |= {x if isinstance(x, str) else x[0] for x in v}
    weekly_only = ({c.name for c in PropertyForSale.__table__.columns}
                   - set(re.findall(r"prop\.([a-z0-9_]+)\s*=", approve_src))
                   - carried)

    # What pricing reads off a listing row — as opposed to the far larger set of
    # names it writes back onto one.
    src = "".join(open(f).read() for f in sorted(glob.glob("app/pricing/*.py")))
    read = set(re.findall(r'\.get\(\s*["\']([a-z0-9_]+)["\']', src))
    read |= set(re.findall(r'getattr\(\s*\w+\s*,\s*["\']([a-z0-9_]+)["\']', src))

    # Three names are read off a row and are not on any file. They are not gaps:
    # pricing produced them itself moments earlier. market_value and confidence
    # come out of comps/glm and are read back by the audit; prior_asking_price is
    # written by the carry-forward, which runs over the live batch whatever the
    # listing's source was.
    produced_here = {"market_value", "confidence", "prior_asking_price"}

    reaching = sorted(read & weekly_only - produced_here)
    assert reaching == [], f"these now reach pricing and are not carried: {reaching}"


def _approved_existing(db, row):
    """Approve a row that already exists, rather than making a new one."""
    from app.portals.listings import approve

    batch = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                        filename="live.csv", is_active=True, status="published")
    db.add(batch)
    db.commit()
    approve(db, row.id, reprice=lambda *a, **k: None)
    return db.query(PropertyForSale).one()
