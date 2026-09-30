"""Delete double-ups among NEW for-sale listings, keyed on the address.

The ingest already drops duplicates inside one upload — but only by SLUG
(_dedupe_by_slug). The same house re-posted under a new campaign comes back with
a new slug and the same front door, and a listing carried forward from last week
can arrive again in this week's file spelled slightly differently. Slug dedupe
misses both, so two rows for one property reach the review grid and, unless
someone spots them, the live site.

This closes that gap the way the rest of the system already recognises "the same
house": addresses.address_key, the exact key PortalListing is deduped on, so
"3/107 Donovan Street" and "3 / 107 Donovan St" collapse into one however each
source spelled it.

TWO RULES, both deliberate:

  It only ever touches the NEW batch — the staged one being reviewed, or the live
  one if that is the only for-sale book there is. The established book is not
  re-scrubbed; "the only thing we are cleaning is new listings."

  Because the staged batch already contains the still-advertised listings carried
  forward from the live book, collapsing within it also removes a new-file row
  that duplicates one ALREADY on the platform — without ever deleting the
  platform's own row.

The survivor is the most useful copy: priced first, then most complete, then the
one with an advertised price, then the newest. Every deletion is written to the
run log with the address, because a row about to stop existing that nobody can
account for later is worse than the duplicate was. Dry-run by default: it reports
exactly what it WOULD remove and writes nothing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from addresses import address_key
from models import BatchType, ImportBatch, PropertyForSale
from runlog import record as _record

log = logging.getLogger("dedupe_forsale")


@dataclass
class DedupeResult:
    batch_id: int | None
    dry_run: bool
    groups: int = 0            # address keys that had more than one row
    duplicates: int = 0        # rows that are duplicates (total rows - survivors)
    removed: int = 0           # rows actually deleted (0 on a dry run)
    examples: list[str] = field(default_factory=list)
    note: str = ""


def _completeness(p: PropertyForSale) -> tuple:
    """How useful this copy is, highest wins. Priced beats unpriced, then more
    filled attributes, then having an advertised price, then the newest row (a
    later load carries refreshed data)."""
    filled = sum(1 for v in (p.floor_area_m2, p.land_area_m2, p.cv_numeric,
                             p.beds, p.baths, p.zoning) if v)
    return (
        1 if p.fair_value is not None else 0,
        filled,
        1 if p.asking_price else 0,
        p.id or 0,
    )


def _target_batch(db: Session, region: str, batch_id: int | None) -> ImportBatch | None:
    if batch_id is not None:
        return db.get(ImportBatch, batch_id)
    # Prefer the staged batch (the one about to go live); fall back to live only
    # if nothing is staged. Never reach back into archived books.
    staged = (db.query(ImportBatch)
              .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                      ImportBatch.region == region,
                      ImportBatch.status.in_(("staged", "preview")))
              .order_by(ImportBatch.id.desc()).first())
    if staged is not None:
        return staged
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                    ImportBatch.region == region,
                    ImportBatch.is_active.is_(True))
            .order_by(ImportBatch.id.desc()).first())


def dedupe_forsale_batch(db: Session, *, region: str = "Auckland",
                         batch_id: int | None = None,
                         dry_run: bool = True) -> DedupeResult:
    """Collapse address-key duplicates within one for-sale batch. See module docs."""
    batch = _target_batch(db, region, batch_id)
    if batch is None:
        return DedupeResult(batch_id=None, dry_run=dry_run,
                            note="No new for-sale batch to clean.")

    rows = (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch.id)
            .order_by(PropertyForSale.id).all())

    by_key: dict[str, list[PropertyForSale]] = {}
    for p in rows:
        key = address_key(p.address, p.suburb)
        if not key:
            continue                                # can't identify it — leave alone
        by_key.setdefault(key, []).append(p)

    res = DedupeResult(batch_id=batch.id, dry_run=dry_run)
    to_delete: list[PropertyForSale] = []
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        res.groups += 1
        survivor = max(group, key=_completeness)
        losers = [p for p in group if p.id != survivor.id]
        res.duplicates += len(losers)
        to_delete.extend(losers)
        if len(res.examples) < 20:
            res.examples.append(
                f"{survivor.address or key}: kept #{survivor.id}, "
                f"removed {', '.join('#' + str(p.id) for p in losers)}")

    if not to_delete:
        res.note = f"No duplicates found in batch {batch.id}."
        return res

    if dry_run:
        res.note = (f"{res.duplicates:,} duplicate row(s) across {res.groups:,} "
                    f"address(es) would be removed from batch {batch.id}. "
                    f"Nothing was deleted (dry run).")
        return res

    # Record a summary, plus each deletion up to a cap, before the rows are gone.
    _record(db, stage="dedupe", event="dedupe_run", batch_id=batch.id,
            count=res.duplicates, level="warn",
            detail=(f"{res.duplicates:,} duplicate listing(s) across {res.groups:,} "
                    f"address(es) removed from batch {batch.id} (kept the most "
                    f"complete copy of each)"))
    for i, p in enumerate(to_delete):
        if i < 200:
            _record(db, stage="dedupe", event="listing_deduped", batch_id=batch.id,
                    address=p.address, count=1, commit=False,
                    detail=f"{p.address or 'a listing'} #{p.id} removed as a duplicate")
        db.delete(p)
        res.removed += 1
    db.commit()
    res.note = (f"Removed {res.removed:,} duplicate row(s) across {res.groups:,} "
                f"address(es) from batch {batch.id}.")
    log.warning("dedupe_forsale: %s", res.note)
    return res
