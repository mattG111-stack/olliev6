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
# Sales are swept WEEKLY, not nightly. A week-old sale is still a comp, where a
# week-old listing is often already under offer — and the cadence is what makes
# it about $13 a month for both portals instead of $90.
SOLD_EVERY_DAYS = 7
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


def sweep_new_listings() -> dict:
    """Ask the portals what went on the market in the last day.

    Separate from the enrichment pass below and deliberately first: this is the
    one that is time-sensitive. The weekly file reaches a Tuesday listing the
    following Monday, and an underpriced listing is under offer inside a week.

    Nothing it finds goes live. Each new listing waits for someone to approve
    it — see portals/listings.py. Never raises; this runs unattended.
    """
    from .listings import sweep

    db = SessionLocal()
    try:
        got = sweep(db)
        found = sum(v["new"] for v in got.values())
        if found:
            log.warning("daily sweep: %d new listings waiting for review", found)
        return got
    except Exception:                             # noqa: BLE001
        log.exception("daily new-listing sweep failed")
        return {}
    finally:
        db.close()


def sweep_sold_listings() -> dict:
    """The weekly pass for sales. Never raises; this runs unattended."""
    from .listings import sweep_sold

    db = SessionLocal()
    try:
        got = sweep_sold(db)
        found = sum(v["new"] for v in got.values())
        if found:
            log.warning("weekly sold sweep: %d sales waiting for review", found)
        return got
    except Exception:                             # noqa: BLE001
        log.exception("weekly sold sweep failed")
        return {}
    finally:
        db.close()


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
        day = 0
        while True:
            try:
                log.warning("daily portal pass starting (%s)",
                            datetime.now(timezone.utc).isoformat(timespec="seconds"))
                # New listings first — the time-sensitive half. A failure in
                # either must not stop the other, so they are caught separately.
                sweep_new_listings()
                # Sales once a week. Counted in passes rather than read off the
                # calendar, so a restart cannot land two sweeps in one day.
                if day % SOLD_EVERY_DAYS == 0:
                    sweep_sold_listings()
                day += 1
                run_once()
            except Exception:                     # noqa: BLE001
                log.exception("daily portal loop")
            time.sleep(every)

    threading.Thread(target=loop, daemon=True, name="portals-daily").start()
    log.warning("daily portal pass is ON — every %.0f hours", every / 3600)
