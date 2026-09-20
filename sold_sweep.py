"""Take a listing off the site once the house turns up in the sold file.

    "ive just noticed alot of houses have sold in the last few weeks and we
     still have them on the site shouldnt they be gone ?"

They should, and the link check was never going to catch them.

WHY THE EXISTING CHECK MISSES THESE ENTIRELY. delisted.py asks whether the
advertisement is still up, and treats only 404 and 410 as evidence. That is the
right rule for what it does. But a New Zealand portal does not delete a page
when a house sells — it leaves it up with a SOLD banner across it, answering
200 for ever. So the one signal we watch for is the one signal a sold house
never sends, and the listing sits there until somebody replaces the whole batch.

Meanwhile the sale arrives in the weekly sold file with an address, a price and
a date on it. Direct evidence rather than an inference from silence, and we have
had it the whole time.

THE TRAP THIS HAS TO AVOID, AND IT IS A BIG ONE. The sold file is a HISTORY. It
holds the 1998 sale, the 2007 sale and the 2019 sale of the same house. Match on
the address alone and every listing whose property has ever changed hands gets
retired — which is nearly all of them, and the site empties overnight with no
error anywhere. So the sale must be NEWER than the listing: a house we have been
advertising since March, with a sale dated April, has sold. A house we started
advertising last week, with a sale dated 2019, has not.

For a listing with no first-seen date — rows loaded before that column existed —
the same question is asked of the calendar instead: only a sale inside
RECENT_SALE_DAYS counts. Older rows are the ones most likely to be stale, so
this must not simply skip them.

WHAT IT DOES NOT DO. It never deletes, and it never writes to the sold record.
It marks the listing, which hides it, and an operator can see exactly which sale
it matched on.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from addresses import address_key
from ingest import as_utc, canonical_sale_date, sold_batch_ids
from models import BatchType, ImportBatch, PropertyForSale, PropertySold

# How far back a sale can be and still retire a listing that has no first-seen
# date. Four months: long enough to cover a settlement lag on a sale agreed
# before we started watching, short enough that an ordinary historical sale on a
# freshly listed house does not reach it.
RECENT_SALE_DAYS = 120

# A sale dated slightly BEFORE we first saw the advertisement is still this
# sale: portals leave a listing up through the unconditional period, and the
# sold file often carries the agreement date rather than the settlement. Without
# this the commonest case of all — sold a few days after we first indexed it —
# would be missed on a technicality.
BEFORE_FIRST_SEEN_GRACE_DAYS = 30


def _live_batch(db: Session, region: str) -> ImportBatch | None:
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                    ImportBatch.region == region,
                    ImportBatch.is_active.is_(True))
            .order_by(ImportBatch.id.desc()).first())


def _sales_by_property(db: Session, region: str) -> dict[str, list[str]]:
    """Every sale we hold, keyed by the property it happened to.

    Address rather than slug. The sold file and the for-sale file come from
    different places and a slug that matches across them is luck; a street
    address is what both of them are actually about.
    """
    ids = sold_batch_ids(db, region)
    if not ids:
        return {}
    out: dict[str, list[str]] = {}
    for address, suburb, sold_date in db.query(
            PropertySold.address, PropertySold.suburb,
            PropertySold.sold_date).filter(
            PropertySold.import_batch_id.in_(ids)).all():
        key = address_key(address, suburb)
        when = canonical_sale_date(sold_date)
        if key and when:
            out.setdefault(key, []).append(when)
    return out


def sweep(db: Session, region: str = "Auckland") -> dict:
    """Mark live listings whose house appears in the sold file with a new sale.

    Returns counts, including how many were considered and skipped, because a
    sweep that changes nothing and a sweep that had nothing to look at are
    different states and only one of them is a problem.
    """
    out = {"checked": 0, "matched": 0, "already": 0, "too_old": 0, "no_sale": 0}
    batch = _live_batch(db, region)
    if batch is None:
        return out
    sales = _sales_by_property(db, region)
    if not sales:
        return out

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=RECENT_SALE_DAYS)).date().isoformat()

    rows = (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch.id).all())
    for p in rows:
        out["checked"] += 1
        if p.sold_seen_at is not None:
            out["already"] += 1
            continue
        key = address_key(p.address, p.suburb)
        when_list = sales.get(key) if key else None
        if not when_list:
            out["no_sale"] += 1
            continue

        seen = as_utc(p.first_seen_at)
        if seen is not None:
            # A sale newer than the day we first advertised it. The grace window
            # covers the sale agreed just before we indexed the listing.
            floor = (seen - timedelta(days=BEFORE_FIRST_SEEN_GRACE_DAYS)) \
                .date().isoformat()
        else:
            floor = cutoff

        hit = max((w for w in when_list if w >= floor), default=None)
        if hit is None:
            out["too_old"] += 1
            continue

        p.sold_seen_at = now
        p.sold_record_date = hit
        out["matched"] += 1

    db.flush()
    return out


def run_once(region: str = "Auckland") -> dict:
    """Entry point for the scheduled worker."""
    from db import SessionLocal

    with SessionLocal() as db:
        got = sweep(db, region=region)
        db.commit()
    print(f"  [sold sweep] {got['matched']:,} listing(s) have sold and were "
          f"taken off, out of {got['checked']:,} live")
    return got
