"""Delete ALL for-sale property data from the database.

Wipes every row in `properties_for_sale` and every `for_sale` row in
`import_batches`, so the next ingest starts from a clean slate. Safe to run
more than once — if there's nothing left to delete it just reports zeros.

  python scripts/delete-for-sale-data.py
  python -m scripts.delete-for-sale-data   # if your shell/module loader allows hyphenated modules

Note: properties are deleted before batches (properties_for_sale.import_batch_id
is a FK into import_batches with no ON DELETE CASCADE), so the order here matters.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal          # noqa: E402
from app.models import BatchType, ImportBatch, PropertyForSale  # noqa: E402


def main() -> None:
    db = SessionLocal()
    try:
        properties_deleted = db.query(PropertyForSale).delete(synchronize_session=False)
        batches_deleted = (
            db.query(ImportBatch)
            .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value)
            .delete(synchronize_session=False)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("Deleted for-sale data:")
    print(f"  properties_for_sale rows deleted: {properties_deleted}")
    print(f"  import_batches (for_sale) rows deleted: {batches_deleted}")


if __name__ == "__main__":
    main()
