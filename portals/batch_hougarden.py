"""Independent, resumable HouGarden enrichment for uploaded batches."""
import json
from datetime import datetime, timezone
from sqlalchemy import text
from db import SessionLocal, engine
from models import IngestJob, PortalListing, PropertyForSale, PortalFinding
from portals import hougarden_backfill as source
from portals.complete import _blank
from staged_stages import _update

STAGE = 'hougarden_enrich'
MAPPING = {f: ('cars' if f == 'carspaces' else f) for f in source.FIELDS
           if f != 'image_count' and hasattr(PropertyForSale, 'cars' if f == 'carspaces' else f)}


def adapter(prop):
    row = PortalListing(source='hougarden', kind='for_sale', address=prop.address,
                        suburb=prop.suburb, district=prop.district,
                        url=source.property_url(prop.url),
                        raw_json='{}')
    for field, column in MAPPING.items():
        setattr(row, field, getattr(prop, column))
    return row


def save_result(db, pid, batch_id, item):
    # Reload only after network I/O. CoreLogic uses the same row lock so neither
    # job fills from a stale snapshot while the other is saving a newer fact.
    prop = db.query(PropertyForSale).filter_by(id=pid, import_batch_id=batch_id).populate_existing().with_for_update().one_or_none()
    if prop is None:
        return 0
    row = adapter(prop)
    _, status = source.apply(row, item)
    if status != 'ok':
        return 0
    raw = json.loads(row.raw_json)
    fields = raw['hougarden_backfill']['filled_fields']
    for field in fields:
        if field not in MAPPING:
            continue
        column = MAPPING[field]
        value = getattr(row, field)
        setattr(prop, column, value)
        db.add(PortalFinding(property_id=pid, batch_id=batch_id, source='hougarden',
                            field=column, kind='fact', status='approved',
                            value_num=float(value) if isinstance(value, (int, float)) else None,
                            value_text=None if isinstance(value, (int, float)) else str(value)[:255],
                            extra_json=json.dumps({'url': item['url'], 'evidence': item}),
                            decided_at=datetime.now(timezone.utc)))
    for field, values in raw.get('source_conflicts', {}).items():
        db.add(PortalFinding(property_id=pid, batch_id=batch_id, source='hougarden',
                            field=MAPPING.get(field, field), kind='conflict', status='rejected',
                            extra_json=json.dumps({'values': values, 'url': item['url'], 'evidence': item})))
    return sum(f in MAPPING for f in fields)


def run_pending():
    with engine.connect() as connection:
        pg = connection.dialect.name == 'postgresql'
        if pg:
            if not connection.execute(text('SELECT pg_try_advisory_lock(792634907,0)')).scalar():
                return
            connection.commit()
        try:
            with SessionLocal() as db:
                job = db.query(IngestJob).filter(IngestJob.filename.like(STAGE+' (batch %'), IngestJob.status.in_(('pending','running','paused'))).order_by(IngestJob.id).first()
                if job is None:
                    return
                core = db.query(IngestJob).filter(IngestJob.batch_id == job.batch_id,
                    IngestJob.filename == f'enrich (batch {job.batch_id})').order_by(IngestJob.id.desc()).first()
                if core is None or core.status != 'completed':
                    _update(db, job.id, status='paused', stage='Waiting for CoreLogic')
                    return
                state = json.loads(job.result_json or '{}')
                if 'ids' not in state:
                    return
                ids = state['ids']
                index = state.get('index', 0)
                filled = state.get('filled', 0)
                misses = state.get('misses', 0)
                _update(db, job.id, status='running', stage=STAGE, started_at=job.started_at or datetime.now(timezone.utc))
                for pid in ids[index:index+10]:
                    prop = db.get(PropertyForSale, pid)
                    if prop is not None and prop.import_batch_id == job.batch_id:
                        row = adapter(prop)
                        result = source.fill(db, row)
                        if result == (0, 'paused'):
                            db.commit()  # preserve shared source cooldown
                            _update(db, job.id, stage='HouGarden paused; will resume')
                            return
                        if result and result[1] == 'ok':
                            item = json.loads(row.raw_json)['hougarden_backfill']['evidence']
                            filled += save_result(db, pid, job.batch_id, item)
                        else:
                            misses += 1
                    index += 1
                    state.update(index=index, filled=filled, misses=misses)
                    _update(db, job.id, rows_total=len(ids), rows_inserted=index,
                            rows_filled=filled, rows_missed=misses,
                            progress_pct=int(100*index/max(1,len(ids))), result_json=json.dumps(state))
                if index >= len(ids):
                    _update(db, job.id, status='completed', stage='hougarden_enrich', progress_pct=100, completed_at=datetime.now(timezone.utc))
        finally:
            if pg:
                connection.rollback()
                connection.execute(text('SELECT pg_advisory_unlock(792634907,0)'))
                connection.commit()
