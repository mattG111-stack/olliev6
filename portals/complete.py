"""Fill in what the portal didn't carry, before anyone decides.

    "where is the rest of the info for these houses ?"

A portal advertises a listing; it does not publish a council record. So a
scraped row arrives with an address, an asking price and a floor area, and no
council valuation — and a listing with no CV cannot be valued at all. Every
valuation method here is anchored on it.

Until now the only chance to fill that gap came AFTER approval, when the row was
already in the live batch, and only if it happened to land in the enrich work
list. So the decision — put this in front of customers or not — was made on the
thinnest version of the row, and a listing marked "no council record" was being
approved into a hold.

This asks the council-record lookup about the pending rows first. Same lookup
the enrich stage uses, so a row filled here is filled the same way a weekly-file
row is.

Three rules, and they are the whole design:

  IT ONLY FILLS BLANKS. What the portal published stays. The portal is looking
  at the actual listing; the lookup is matching on an address string and can
  match the wrong house. Where they disagree, the listing wins.

  IT NEVER TOUCHES THE PRICE. The asking price is the one fact a portal is
  authoritative about, and a council record has no opinion on it.

  IT DOES NOT APPROVE ANYTHING. A filled row is still pending. Filling is what
  makes the decision possible, not the decision.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import PortalListing
from ..propertyvalue import PV_OK, pv_lookup_status

log = logging.getLogger(__name__)

# What a council record can honestly supply for a listing. Deliberately not
# price_numeric, and not address/suburb — a lookup that rewrote the address it
# was given would be matching one house and describing another.
_FILLABLE = (
    ("cv_numeric", "cv"),
    ("land_value_numeric", "land_value"),
    ("improvement_value_numeric", "improvement_value"),
    ("floor_area_m2", "floor_area_m2"),
    ("land_area_m2", "land_area_m2"),
    ("beds", "beds"),
    ("baths", "baths"),
    # THEIR key is car_spaces; ours is carspaces. Spelled wrong this fills
    # nothing and says nothing — the quietest kind of failure there is.
    ("carspaces", "car_spaces"),
    # The last sale. One of only two fields a portal row was missing that
    # actually reaches the pricing engine.
    ("prior_sale_price", "last_sale_price"),
    ("prior_sale_date", "last_sale_date"),
)


def _blank(v) -> bool:
    if isinstance(v, str):
        return not v.strip()
    return v is None or v == 0 or (isinstance(v, float) and v != v)


def needs_filling(row: PortalListing) -> bool:
    """Would a lookup add anything? A row with everything already is not worth
    the request, and a weekly load of these adds up."""
    return any(_blank(getattr(row, ours, None)) for ours, _ in _FILLABLE)


def fill_one(db: Session, row: PortalListing) -> tuple[int, str]:
    """(fields filled, status). Commits nothing — the caller batches that."""
    if not row.address:
        return 0, "no address"
    q = ", ".join(x for x in (row.address, (row.suburb or "").strip(), "Auckland")
                  if x and x.lower() != "nan")
    rec, status = pv_lookup_status(q)
    if status != PV_OK or not rec:
        return 0, status

    filled = 0
    for ours, theirs in _FILLABLE:
        if _blank(getattr(row, ours, None)) and not _blank(rec.get(theirs)):
            setattr(row, ours, rec.get(theirs))
            filled += 1
    return filled, status


# How many pending rows one call may look up. Each one is a real request to the
# council-record service taking a second or more, so 400 in a single call is
# minutes of work on a request thread — and the gateway cuts the connection long
# before it finishes. The browser then sees "HTTP 500 with no response body",
# which reads as a server fault and is really a timeout. Small enough to answer
# well inside any proxy's patience; the caller repeats until there is nothing
# left, exactly as the portal sweep already does.
CHUNK = 25
MAX_CHUNK = 100


def fill_pending(db: Session, *, kind: str = "for_sale",
                 limit: int = CHUNK, after_id: int = 0) -> dict:
    """Fill the gaps on one chunk of pending rows, and say what is left.

    Returns counts in the shape the page reads, and the same words the enrich
    stage uses — filled / not found / unreachable — so two screens describing
    the same lookup do not describe it differently.

    Walks forward by id rather than re-querying "rows that still need filling".
    A row that was looked up and STILL has gaps — because the record was never
    reached — would otherwise be picked again by the next call, and the caller
    would loop on it for ever. `last_id` is the high-water mark of what has been
    SCANNED, whether or not it needed anything, so progress is guaranteed.
    """
    limit = max(1, min(int(limit or CHUNK), MAX_CHUNK))
    scanned = (db.query(PortalListing)
               .filter(PortalListing.status == "pending",
                       PortalListing.kind == kind,
                       PortalListing.id > int(after_id or 0))
               .order_by(PortalListing.id).limit(limit).all())
    rows = [r for r in scanned if needs_filling(r)]
    last_id = scanned[-1].id if scanned else int(after_id or 0)

    looked = fields = not_found = unreachable = blocked = 0
    with_cv_before = sum(1 for r in rows if not _blank(r.cv_numeric))
    for r in rows:
        looked += 1
        try:
            n, status = fill_one(db, r)
        except Exception as e:                       # noqa: BLE001
            log.info("filling a pending listing failed for %s: %s", r.id, e)
            unreachable += 1
            continue
        fields += n
        if status == "not_found":
            not_found += 1
        elif status == "blocked":
            blocked += 1
        elif status != PV_OK:
            unreachable += 1
        if looked % 25 == 0:
            db.commit()
    db.commit()

    with_cv_after = sum(1 for r in rows if not _blank(r.cv_numeric))
    remaining = (db.query(PortalListing)
                 .filter(PortalListing.status == "pending",
                         PortalListing.kind == kind,
                         PortalListing.id > last_id).count())
    return {
        "looked_up": looked,
        "fields_filled": fields,
        # The number that decides whether these rows can be priced at all.
        "council_records_found": with_cv_after - with_cv_before,
        "not_found": not_found,
        "blocked": blocked,
        "unreachable": unreachable,
        # Where to carry on from, and how much is left. `scanned` being empty is
        # the only stop condition the caller needs — `remaining` is for the
        # progress line, not for deciding when to stop.
        "last_id": last_id,
        "remaining": remaining,
        "scanned": len(scanned),
    }
