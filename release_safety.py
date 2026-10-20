"""Publication checks and private working copies; no automatic publication."""
import hashlib
import json
from sqlalchemy import inspect, text
from models import ImportBatch, PropertyForSale, IngestJob, PortalFinding


def lock_release(db, region):
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                   {"key": f"ollie:publish:{region}"})


def working_copy(db, region):
    from release import _staged_batch
    lock_release(db, region)
    staged = _staged_batch(db, 'for_sale', region)
    if staged is not None:
        return staged
    live = db.query(ImportBatch).filter_by(batch_type='for_sale', region=region,
        status='published', is_active=True).order_by(ImportBatch.id.desc()).first()
    if live is None:
        return None
    draft = ImportBatch(batch_type='for_sale', region=region, status='staged',
        is_active=False, filename=f'Review copy of batch {live.id}', rows_total=0, rows_inserted=0)
    db.add(draft); db.flush()
    fields=[p.key for p in inspect(PropertyForSale).column_attrs if p.key not in ('id','import_batch_id')]
    mapping={}
    originals = db.query(PropertyForSale).filter_by(import_batch_id=live.id).all()
    for start in range(0, len(originals), 250):
        chunk = originals[start:start+250]
        copies = [PropertyForSale(import_batch_id=draft.id, **{k:getattr(old,k) for k in fields}) for old in chunk]
        db.add_all(copies); db.flush()
        mapping.update({old.id:copy.id for old,copy in zip(chunk,copies)})
    for start in range(0,len(mapping),500):
        old_ids=list(mapping)[start:start+500]
        for f in db.query(PortalFinding).filter(PortalFinding.property_id.in_(old_ids)):
            values={p.key:getattr(f,p.key) for p in inspect(PortalFinding).column_attrs
                    if p.key not in ('id','property_id','batch_id')}
            db.add(PortalFinding(property_id=mapping[f.property_id],batch_id=draft.id,**values))
    draft.rows_total=draft.rows_inserted=len(mapping)
    db.flush()
    return draft


def pricing_fingerprint(db, batch_id):
    from reprice import _row_to_input
    digest=hashlib.sha256()
    outputs=('fair_value','buy_price','margin','market_value','comps_used','confidence',
             'best_net_gain','expected_sale_path','asking_basis')
    for p in db.query(PropertyForSale).filter_by(import_batch_id=batch_id).order_by(PropertyForSale.id).yield_per(250):
        payload={'id':p.id,'inputs':_row_to_input(p),
                 'outputs':{k:getattr(p,k,None) for k in outputs}}
        digest.update(json.dumps(payload,sort_keys=True,default=str,allow_nan=False).encode())
    return digest.hexdigest()


def validate_release(db, region):
    from release import _staged_batch, _hold_reason
    lock_release(db, region)
    sold = _staged_batch(db, 'sold', region)
    if sold and db.query(IngestJob.id).filter(IngestJob.batch_id == sold.id,
            IngestJob.status.in_(('pending','running','paused'))).first():
        raise ValueError('Sold data is still processing; wait before approving it.')
    batch=_staged_batch(db,'for_sale',region)
    if batch is None:
        return
    db.query(ImportBatch).filter_by(id=batch.id).with_for_update().one()
    if db.query(IngestJob.id).filter(IngestJob.batch_id==batch.id,
            IngestJob.status.in_(('pending','running','paused')),
            IngestJob.stage != 'auto_pipeline').first():
        raise ValueError('This batch has queued or running work, or a paused source. Finish or stop it before publishing.')
    price=db.query(IngestJob).filter(IngestJob.batch_id==batch.id,
        IngestJob.filename.like('price (%')).order_by(IngestJob.id.desc()).first()
    try:
        payload=json.loads(price.result_json or '{}') if price else {}
    except (ValueError,TypeError):
        payload={}
    if not isinstance(payload,dict):
        payload={}
    if price is None or price.status!='completed' or not payload.get('review_fingerprint'):
        raise ValueError('Run pricing on this batch before publishing. Older runs need one fresh validation run.')
    if payload['review_fingerprint']!=pricing_fingerprint(db,batch.id):
        raise ValueError('Pricing inputs or results changed. Run pricing again before publishing.')
    import math
    for p in db.query(PropertyForSale).filter_by(import_batch_id=batch.id):
        reason = _hold_reason(p)
        if (p.fair_value is None or not math.isfinite(p.fair_value) or p.fair_value <= 0):
            reason = 'Missing or invalid valuation; reprice or exclude from publication'
        if reason:
            p.is_held = True
            p.hold_reason = reason
    # Never clear existing review holds just because the numerical rules pass.
    db.flush()


def require_private_listing(db, listing_id):
    from fastapi import HTTPException
    p = db.get(PropertyForSale, listing_id)
    if p is None:
        raise HTTPException(404, 'Listing not found')
    batch = db.get(ImportBatch, p.import_batch_id)
    if batch is None or batch.status not in ('staged', 'preview') or batch.is_active:
        raise HTTPException(409, 'Create a private review copy using Enrich or Reprice, then edit it in Review before publishing.')
    return p
