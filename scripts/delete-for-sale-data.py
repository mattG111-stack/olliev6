"""Delete all FOR-SALE data — every for-sale listing and its import batches.

Keeps sold data, rentals, users, billing and settings. Use before re-loading a
fresh for-sale CSV, or to clear a bad import.

SAFETY: dry-run by default — it only reports what it WOULD delete. Pass --yes to
actually delete. Do NOT put this in the service Start Command (it would run on
every deploy); run it as a one-off command in Railway instead:

    python scripts/delete-for-sale-data.py --yes

(To wipe everything — sold, rentals, batches too — use the Reset button on the
publish page, or POST /api/admin/reset-all?confirm=RESET.)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal  # noqa: E402
from app.models import BatchType, ImportBatch, PropertyForSale  # noqa: E402


def main() -> int:
    confirm = "--yes" in sys.argv or "-y" in sys.argv
    db = SessionLocal()
    try:
        n_rows = db.query(PropertyForSale).count()
        n_batches = (db.query(ImportBatch)
                     .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value)
                     .count())
        if not confirm:
            print(f"[dry run] Would delete {n_rows} for-sale listing(s) and "
                  f"{n_batches} for-sale batch(es). Sold data, rentals and users "
                  f"are kept. Re-run with --yes to actually delete.")
            return 0
        deleted = db.query(PropertyForSale).delete(synchronize_session=False)
        (db.query(ImportBatch)
           .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value)
           .delete(synchronize_session=False))
        db.commit()
        print(f"Deleted {deleted} for-sale listing(s) and {n_batches} for-sale "
              f"batch(es). Sold data, rentals and users are untouched.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
