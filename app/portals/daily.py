"""Ask the portals once a day, without anyone pressing anything.

Twenty to thirty properties clear the margin floor in a week. A daily pass costs
four or five lookups a day, which is why this can run unattended at all — and
why it stays cheap: a property asked about within PORTAL_TTL_DAYS is skipped, so
a daily run touches the ones that arrived since the last one and nobody else.

Runs over the LIVE batch, because that is the one customers are looking at. Any
fill that changes what a property is worth goes back through the same pricing
and the same hold rules the weekly release uses — see run_portal_job. A portal
must not be able to move a listing across the margin floor without the floor
being re-checked.

Off unless PORTALS_DAILY is set. A background job that reaches the internet and
spends money should be something someone turned on, not something that starts
because a deploy went out.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from ..config import settings
from ..db import SessionLocal
from ..models import BatchType, ImportBatch
from ..staged_stages import create_stage_job
from .runner import run_portal_job

log = logging.getLogger(__name__)

EVERY_SECONDS = 24 * 60 * 60
# A daily pass should be a handful of properties. If it ever wants hundreds,
# something upstream has changed and a human should look before we spend it.
DAILY_CAP = 60
# Long enough after boot that a deploy, a restart loop or a rollback does not
# fire a run each time.
FIRST_RUN_AFTER = 15 * 60


def enabled() -> bool:
    return bool(getattr(settings, "portals_daily", False))


def _live_batch_id(db) -> int | None:
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    return b.id if b else None


def run_once(*, cap: int = DAILY_CAP) -> int | None:
    """One pass over the live batch. Returns the job id, or None if there was
    nothing to do. Never raises — this runs unattended."""
    db = SessionLocal()
    try:
        batch_id = _live_batch_id(db)
        if not batch_id:
            return None
        job = create_stage_job(db, stage="portals", batch_id=batch_id,
                               region="Auckland", uploaded_by_id=None)
        jid = job.id
    except Exception:                             # noqa: BLE001
        log.exception("daily portal pass could not start")
        return None
    finally:
        db.close()

    try:
        run_portal_job(jid, batch_id, cap=cap)
    except Exception:                             # noqa: BLE001
        log.exception("daily portal pass failed")
    return jid


def start(*, every: int = EVERY_SECONDS, first_after: int = FIRST_RUN_AFTER) -> None:
    """Start the daily loop on a daemon thread. No-op when switched off."""
    if not enabled():
        return

    def loop() -> None:
        time.sleep(first_after)
        while True:
            try:
                log.warning("daily portal pass starting (%s)",
                            datetime.now(timezone.utc).isoformat(timespec="seconds"))
                run_once()
            except Exception:                     # noqa: BLE001
                log.exception("daily portal loop")
            time.sleep(every)

    threading.Thread(target=loop, daemon=True, name="portals-daily").start()
    log.warning("daily portal pass is ON — every %.0f hours", every / 3600)
