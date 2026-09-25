"""Durable successful-run timestamps and a PostgreSQL cross-worker lock."""
import hashlib
import threading
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session
from models import AppSetting

_local_lock=threading.Lock()


def run_due(engine, name, every, fn, *, now=None):
    now=now or datetime.now(timezone.utc)
    key='scraper.schedule.'+hashlib.sha256(name.encode()).hexdigest()[:32]
    lock_key=int.from_bytes(hashlib.sha256(key.encode()).digest()[:8],'big',signed=True)
    with engine.connect() as conn:
        postgres=conn.dialect.name=='postgresql'
        locked=bool(conn.execute(text('SELECT pg_try_advisory_lock(:key)'),{'key':lock_key}).scalar()) if postgres else _local_lock.acquire(blocking=False)
        if not locked:return False
        try:
            conn.commit()
            with Session(bind=conn) as db:
                state=db.get(AppSetting,key)
                if state and state.value:
                    last=datetime.fromisoformat(state.value)
                    if (now-last).total_seconds()<every:return False
                db.commit()
                result=fn()
                if result=={}:raise RuntimeError('Scheduled collector reported failure')
                # Mark success only after work finishes. A killed process releases
                # its database lock and the next attempt replays idempotently.
                state=db.get(AppSetting,key)
                if state is None:state=AppSetting(key=key);db.add(state)
                state.value=now.isoformat();db.commit()
                return True
        finally:
            if postgres:
                conn.rollback()
                conn.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':lock_key});conn.commit()
            else:_local_lock.release()
