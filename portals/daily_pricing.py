"""Atomic daily valuation refresh using published sold evidence only."""
from sqlalchemy import text
from models import ImportBatch, BatchType, PropertyForSale
from reprice import reprice_batch


def enabled():
    from config import settings
    return settings.scraper_daily_pricing


def run_once():
    from db import SessionLocal
    from models import PortalCollectionRun
    from datetime import datetime, timezone
    import json
    with SessionLocal() as db:
        ids=[b.id for b in db.query(ImportBatch.id).filter_by(is_active=True,status='published',batch_type=BatchType.FOR_SALE.value).all()]
    for bid in ids:
        failed=False
        with SessionLocal() as db:
            audit=PortalCollectionRun(kind='pricing',status='running')
            db.add(audit);db.commit();audit_id=audit.id
            try:
                result=reprice_live(db,bid)
                audit=db.get(PortalCollectionRun,audit_id)
                audit.status='complete'
                audit.summary_json=json.dumps({'batch_id':bid,'rows':result.rows,'changed':result.changed_fair_value})
            except Exception as exc:
                failed=True
                audit=db.get(PortalCollectionRun,audit_id)
                audit.status='failed';audit.error_type=type(exc).__name__
                audit.summary_json=json.dumps({'batch_id':bid})
            audit.finished_at=datetime.now(timezone.utc);db.commit()
            if failed:raise RuntimeError('Daily pricing failed; see collection-run audit')


def reprice_live(db, batch_id, *, chunk=500):
    """Requires a dedicated session. One commit publishes the entire result."""
    try:
        if db.bind.dialect.name=='postgresql':
            db.execute(text('LOCK TABLE properties_for_sale IN SHARE ROW EXCLUSIVE MODE'))
            db.execute(text('LOCK TABLE properties_sold IN SHARE MODE'))
        batch=db.query(ImportBatch).filter_by(id=batch_id,is_active=True,status='published',batch_type=BatchType.FOR_SALE.value).with_for_update().first()
        if batch is None:raise ValueError('Daily pricing requires an active published for-sale batch')
        result=reprice_batch(db,batch_id,region=batch.region or 'Auckland',commit=True,
            chunk=chunk,commit_chunks=False,published_only=True)
        if result.error:raise ValueError(result.error)
        from release import _hold_reason
        for prop in db.query(PropertyForSale).filter_by(import_batch_id=batch_id).yield_per(chunk):
            reason=_hold_reason(prop)
            prop.is_held=bool(reason);prop.hold_reason=reason
        db.commit();result.committed=True
        return result
    except Exception:
        db.rollback()
        raise
