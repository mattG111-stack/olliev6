"""Durable admin-requested CoreLogic/pricing jobs, drained by the worker only."""
import json
import logging
from datetime import datetime, timezone
from sqlalchemy import text
from db import engine, SessionLocal
from models import IngestJob, ImportBatch


def queue_stage(db, job_id, stage, region):
    if stage not in ('enrich', 'price'):
        raise ValueError('Unsupported requested stage')
    job = db.get(IngestJob, job_id)
    batch = db.get(ImportBatch, job.batch_id)
    if batch is None or batch.status not in ('staged', 'preview') or batch.is_active:
        raise ValueError('Requested work requires a private batch')
    payload = json.loads(job.result_json or '{}')
    payload['requested_stage'] = {'version': 1, 'stage': stage, 'region': region}
    job.result_json = json.dumps(payload)
    db.commit()


def run_pending():
    # Session lock survives progress commits and drops when the worker dies.
    # A second replica cannot replay the same paid source work concurrently.
    with engine.connect() as connection:
        acquired = False
        try:
            if connection.dialect.name == 'postgresql':
                acquired = bool(connection.execute(text('SELECT pg_try_advisory_lock(792634912, 0)')).scalar())
                connection.commit()
                if not acquired:
                    return
            with SessionLocal() as db:
                jobs = db.query(IngestJob).filter(IngestJob.status.in_(('pending','running')),
                    IngestJob.result_json.contains('requested_stage')).order_by(IngestJob.id).all()
                for job in jobs:
                    payload = json.loads(job.result_json or '{}')
                    request = payload.get('requested_stage', {})
                    if request.get('version') != 1:
                        continue
                    batch = db.get(ImportBatch, job.batch_id)
                    attempts = payload.get('worker_attempts', 0)
                    if attempts >= 3 or batch is None or batch.status not in ('staged','preview') or batch.is_active:
                        job.status = 'failed'
                        job.error_message = 'Worker recovery stopped; review this job before retrying.'
                        job.completed_at = datetime.now(timezone.utc)
                        db.commit()
                        return
                    # Serialise all sources/pricing over this exact batch.
                    other = db.query(IngestJob.id).filter(IngestJob.batch_id == job.batch_id,
                        IngestJob.id != job.id, IngestJob.status == 'running',
                        IngestJob.stage != 'auto_pipeline').first()
                    if other:
                        continue
                    payload['worker_attempts'] = attempts + 1
                    job.result_json = json.dumps(payload)
                    db.commit()
                    jid, bid = job.id, job.batch_id
                    from staged_stages import run_enrich_job, run_price_job
                    target = run_enrich_job if request['stage'] == 'enrich' else run_price_job
                    target(jid, bid, request['region'])
                    return
        finally:
            if acquired:
                connection.rollback()
                connection.execute(text('SELECT pg_advisory_unlock(792634912, 0)'))
                connection.commit()


def poll_requested_stages(*, sleep=None):
    import time
    import worker
    sleep = sleep or time.sleep
    while not worker._stop:
        try:
            run_pending()
        except Exception:
            logging.getLogger(__name__).exception('Requested stage drain failed')
        sleep(10)


def create_requested_stage(db, *, stage, batch_id, region, uploaded_by_id):
    """Create request and its recovery marker in the same locked commit."""
    from release_safety import lock_release
    from fastapi import HTTPException
    lock_release(db, region)
    existing = db.query(IngestJob).filter(IngestJob.batch_id == batch_id,
        IngestJob.filename == f'{stage} (batch {batch_id})',
        IngestJob.status.in_(('pending','running'))).order_by(IngestJob.id.desc()).first()
    if existing:
        return existing
    other = db.query(IngestJob.id).filter(IngestJob.batch_id == batch_id,
        IngestJob.status.in_(('pending','running')),
        IngestJob.stage != 'auto_pipeline').first()
    if other:
        raise HTTPException(409, 'Wait for the current batch work to finish before starting another stage.')
    job = IngestJob(batch_type='for_sale', batch_id=batch_id, stage=stage,
        filename=f'{stage} (batch {batch_id})', status='pending', uploaded_by_id=uploaded_by_id)
    db.add(job); db.flush()
    queue_stage(db, job.id, stage, region)
    return job
