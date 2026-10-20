"""Customer market data is published data; private pricing has separate selectors."""
from sqlalchemy import select
from models import ImportBatch, BatchType


def approved_sold_batch_ids(db, region):
    return [bid for (bid,) in db.query(ImportBatch.id).filter(
        ImportBatch.batch_type == BatchType.SOLD.value,
        ImportBatch.region == region, ImportBatch.status == "published").all()]
