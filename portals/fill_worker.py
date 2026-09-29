"""Consume button-requested fill jobs where the source transport is configured."""
import json
import threading
import time
import logging
from sqlalchemy import text
from db import engine, SessionLocal
from models import IngestJob

_thread = None
_thread_lock = threading.Lock()


def run_pending():
    with engine.connect() as connection:
        postgres = connection.dialect.name == 'postgresql'
        acquired = False
        try:
            if postgres:
                acquired = bool(connection.execute(text(
                    'SELECT pg_try_advisory_lock(792634904, 0)')).scalar())
                connection.commit()
                if not acquired:
                    return
            with SessionLocal() as db:
                job = db.query(IngestJob).filter(
                    IngestJob.filename == 'filling',
                    IngestJob.status.in_(('pending', 'running'))
                ).order_by(IngestJob.id).first()
                if job is None:
                    return
                jid = job.id
                kind = json.loads(job.result_json or '{}').get('kind', 'for_sale')
            from routers.release import _run_fill_job
            _run_fill_job(jid, kind=kind)
        finally:
            if acquired:
                connection.rollback()
                connection.execute(text('SELECT pg_advisory_unlock(792634904, 0)'))
                connection.commit()


def poll_pending(*, sleep=time.sleep):
    """Keep button requests responsive while the main scheduler runs long sweeps."""
    import worker
    from config import settings
    while not worker._stop:
        if settings.scraper_enabled:
            try:
                from portals.batch_hougarden import run_pending as run_hougarden
                run_hougarden()
                run_pending()
            except Exception:
                logging.getLogger(__name__).exception('Requested fill polling failed; will retry')
        sleep(10)


def dispatch():
    # Keep scheduled collection responsive while a requested fill is running.
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=poll_pending, daemon=True)
            _thread.start()
