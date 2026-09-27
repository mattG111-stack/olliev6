"""Portal-only additive review: price privately, remove from review, then publish.

Drafts are deliberately outside CSV staged/preview batches. Publishing moves only
reviewed rows into the active batch; it never replaces the existing property book.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os

from sqlalchemy import func, text
from models import ImportBatch, IngestJob, PortalListing, PropertyForSale
from trademe import address_key

DRAFT = 'portal_review'
JOB = 'portal pricing'


def lock(db):
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(792634901)'))


def download_counts(db, now=None):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo('Pacific/Auckland'))
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())
    # Earliest retained record per property: repeated observations and source
    # merges do not inflate this metric. Unknown-address records are excluded.
    q = db.query(PortalListing.kind, PortalListing.address_key,
                 func.min(PortalListing.created_at)).filter(
        PortalListing.source.in_(('oneroof','trademe','homes')),
        PortalListing.address_key.isnot(None), PortalListing.address_key != '',
        PortalListing.kind.in_(('for_sale','sold'))).group_by(
            PortalListing.kind, PortalListing.address_key)
    result = {k: {'today': 0, 'week': 0} for k in ('for_sale','sold')}
    for kind, _, first in q:
        if first is None: continue
        if first.tzinfo is None: first = first.replace(tzinfo=timezone.utc)
        if first > now: continue
        if first >= week: result[kind]['week'] += 1
        if first >= today: result[kind]['today'] += 1
    return {**result, 'timezone':'Pacific/Auckland', 'week_starts':week.date().isoformat(),
            'as_of':now.isoformat(), 'definition':'Distinct properties first saved from the three portals. Sold includes records awaiting validation; these are downloads, not approved comparable sales.'}


def publication_enabled():
    # The earlier production pricing audit is unresolved. Draft review is usable,
    # but explicitly enabling publication waits for that validation to be cleared.
    return os.getenv('PORTAL_REVIEW_PUBLISH_ENABLED','false').lower() == 'true'


def start(db, ids, user_id):
    lock(db)
    running = db.query(IngestJob).filter(IngestJob.filename == JOB,
        IngestJob.status.in_(('pending','running'))).first()
    if running: return running, False
    if not ids: raise ValueError('Select at least one listing to price')
    batch = db.query(ImportBatch).filter_by(status=DRAFT, region='Auckland').first()
    if batch is None:
        batch = ImportBatch(batch_type='for_sale', filename='Portal pricing review',
            region='Auckland', status=DRAFT, is_active=False, uploaded_by_id=user_id)
        db.add(batch); db.flush()
    import json
    job = IngestJob(batch_type='for_sale', filename=JOB, status='pending', stage='Waiting to price',
        progress_pct=0, rows_total=len(set(ids)), rows_inserted=0,
        result_json=json.dumps({'ids':list(dict.fromkeys(ids)), 'batch_id':batch.id, 'user_id':user_id}))
    db.add(job); db.commit(); db.refresh(job)
    return job, True


def price_one(db, source, batch_id, dataset, rent, model):
    import pandas as pd
    from portals.listings import property_from_listing
    from reprice import _row_to_input, _apply_outputs, run_pipeline
    from release import _hold_reason
    if source.status == 'priced':
        prop = db.get(PropertyForSale, source.property_id)
        if not prop or prop.import_batch_id != batch_id:
            raise ValueError('Only private review drafts can be repriced here')
    else:
        prop = property_from_listing(source, batch_id)
    # Portals differ in casing. Reuse the most common exact spelling in the
    # comparable dataset; never fuzzy-match suburbs or change source evidence.
    for field in ('suburb', 'district'):
        value = getattr(prop, field, None)
        if value and field in dataset.df.columns:
            counts = dataset.df[field].dropna().astype(str).str.strip().value_counts()
            match = next((name for name in counts.index if name.casefold() == str(value).strip().casefold()), None)
            if match: setattr(prop, field, match)
    outputs = run_pipeline(pd.DataFrame([_row_to_input(prop)]), dataset, rent, model=model)
    _apply_outputs(prop, outputs.iloc[0].to_dict())
    reason = source.price_flag or _hold_reason(prop)
    prop.is_held = bool(reason); prop.hold_reason = reason
    db.add(prop); db.flush()
    source.property_id = prop.id; source.status = 'priced'
    return prop


def run(job_id):
    import json
    from db import SessionLocal
    from reprice import _sold_df, _rent_rates
    from pricing.comps import SoldDataset
    from ml.store import live_model
    with SessionLocal() as db:
        job = db.get(IngestJob, job_id)
        payload = json.loads(job.result_json)
        ids, batch_id = payload['ids'], payload['batch_id']
        try:
            job.status='running'; job.started_at=datetime.now(timezone.utc); db.commit()
            sold = _sold_df(db,'Auckland',published_only=True)
            if sold is None or sold.empty: raise ValueError('No published comparable sales available')
            dataset, rent, model = SoldDataset(sold), _rent_rates(db,'Auckland'), live_model(db)
            priced, errors = 0, []
            for i, sid in enumerate(ids):
                try:
                    row = db.query(PortalListing).filter_by(id=sid).with_for_update().first()
                    if row is None or row.kind != 'for_sale' or row.status not in ('pending','priced'):
                        raise ValueError('Listing is no longer awaiting pricing')
                    if row.delisted_at: raise ValueError('Listing is no longer advertised')
                    price_one(db,row,batch_id,dataset,rent,model)
                    row.decided_by_id=payload['user_id']; row.decided_at=datetime.now(timezone.utc)
                    db.commit(); priced += 1
                except Exception as exc:
                    db.rollback()
                    # Keep failed inputs pending so they can be corrected and retried.
                    errors.append({'id':sid,'error':str(exc)[:160] if isinstance(exc,ValueError) else 'Pricing failed; listing retained for retry'})
                job=db.get(IngestJob,job_id)
                job.stage=f'Priced {i+1}/{len(ids)}'; job.progress_pct=min(99,int((i+1)*100/len(ids)))
                job.rows_inserted=priced; job.rows_rejected=len(errors)
                job.result_json=json.dumps({**payload,'errors':errors}); db.commit()
            job.status='completed'; job.progress_pct=100; job.stage=f'{priced} priced · {len(errors)} need attention'
            job.completed_at=datetime.now(timezone.utc); db.commit()
        except Exception as exc:
            db.rollback(); job=db.get(IngestJob,job_id); job.status='failed'
            job.error_message=str(exc)[:200] if isinstance(exc,ValueError) else 'Pricing failed; saved drafts remain available'
            job.completed_at=datetime.now(timezone.utc); db.commit()


def review(db):
    rows = db.query(PortalListing,PropertyForSale).join(PropertyForSale,
        PortalListing.property_id == PropertyForSale.id).join(ImportBatch,
        PropertyForSale.import_batch_id == ImportBatch.id).filter(
        ImportBatch.status==DRAFT, PortalListing.kind=='for_sale',
        PortalListing.status.in_(('priced','removed'))).order_by(PortalListing.id.desc()).all()
    return {'publish_enabled':publication_enabled(), 'rows':[{
        'id':r.id,'address':p.address,'suburb':p.suburb,'asking':p.asking_price,
        'value':p.fair_value,'held':p.is_held,'reason':p.hold_reason,
        'removed':r.status=='removed','image_url':r.image_url,
        'floor':p.floor_area_m2,'land':p.land_area_m2} for r,p in rows]}


def remove(db, sid, removed):
    lock(db)
    row=db.query(PortalListing).filter_by(id=sid).with_for_update().first()
    if row is None or row.status not in ('priced','removed'): raise ValueError('Not a draft listing')
    prop=db.get(PropertyForSale,row.property_id)
    if not prop or db.get(ImportBatch,prop.import_batch_id).status != DRAFT: raise ValueError('Not a draft listing')
    row.status='removed' if removed else 'priced'; db.commit()


def publish(db, ids, user_id):
    if not publication_enabled(): raise ValueError('Live is blocked until the outstanding pricing validation is cleared')
    if not ids: raise ValueError('Select reviewed listings first')
    lock(db)
    live=db.query(ImportBatch).filter_by(batch_type='for_sale',region='Auckland',is_active=True).with_for_update().order_by(ImportBatch.id.desc()).first()
    if live is None: raise ValueError('No active for-sale batch exists')
    from portals.listings import _live_keys
    keys=_live_keys(db)
    prepared=[]
    for sid in set(ids):
        row=db.query(PortalListing).filter_by(id=sid).with_for_update().first()
        if row is None or row.status!='priced': raise ValueError('A selected listing is no longer ready')
        prop=db.get(PropertyForSale,row.property_id)
        if not prop or db.get(ImportBatch,prop.import_batch_id).status!=DRAFT: raise ValueError('Not a draft listing')
        if row.price_flag or row.delisted_at or prop.is_held or not prop.fair_value or prop.fair_value<=0: raise ValueError('Resolve held or unpriced listings before going live')
        key=address_key(prop.address,prop.suburb)
        if not key or key in keys: raise ValueError('A selected property already exists in a current batch')
        keys.add(key); prepared.append((row,prop))
    # Validate the complete selection first, then commit all moves together.
    for row,prop in prepared:
        prop.import_batch_id=live.id; row.status='approved'
        row.decided_by_id=user_id; row.decided_at=datetime.now(timezone.utc)
    db.commit()
    return len(prepared)
