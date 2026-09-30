"""Fail jobs that stopped making a sound, so the upload can never hang forever.

The manual upload runs its LOAD on a background thread inside the API process
(admin_upload.upload_csvs). A daemon thread dies the instant the container is
restarted, redeployed or OOM-killed — and when it dies mid-run the `except` that
would have written status="failed" never executes. The job row is left at
status="running" with no thread behind it, and the upload screen polls it for
ever: "runs but never finishes."

The staged stages (enrich / price / portals) already self-heal — stage_running()
auto-clears a run that has not beaten in ten minutes. Nothing did the same for
the LOAD job, or for a job left "pending" because the thread died before it even
started. This is that safety net, and it runs in the WORKER, which is a separate
process that stays up when the API restarts — so the one thing that could clear a
stuck job is not the same thing that got killed.

Nothing here runs the work. It only turns an invisible dead job into a visible
failed one the operator can retry.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from db import SessionLocal
from models import IngestJob

log = logging.getLogger("job_reaper")

# A job that has been "running" this long without a heartbeat (last_progress_at,
# or started_at for rows written before heartbeats) is treated as dead: its
# container was killed or redeployed mid-run. Generous on purpose — a LOAD that
# prices a full weekly file is one long call with a quiet stretch in the middle,
# and the fault being fixed is jobs stuck for DAYS, not for minutes. Well clear
# of the ten-minute heartbeat the stage jobs beat at.
RUNNING_STUCK_MINUTES = 30

# A job left "pending" this long was never picked up at all — the thread that
# should have run it died before it started. Nothing else will ever start it, so
# it is dead on arrival.
PENDING_STUCK_MINUTES = 20


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes, Postgres aware ones; comparing the two
    raises. Everything stored is UTC, so treat a naive value as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _last_sound(j: IngestJob) -> datetime | None:
    """The most recent moment this job proved it was alive."""
    for value in (getattr(j, "last_progress_at", None), j.started_at, j.created_at):
        aware = _aware(value)
        if aware is not None:
            return aware
    return None


def _cleanup_temp_file(j: IngestJob) -> None:
    """Remove the upload's temp file if the dead thread never got to. Best effort:
    a leaked temp file is untidy, not dangerous, so it must not raise here."""
    try:
        if j.file_path and os.path.exists(j.file_path):
            os.remove(j.file_path)
    except Exception:                              # noqa: BLE001
        pass


def reap_stuck_jobs(db: Session | None = None) -> int:
    """Mark abandoned upload / stage jobs as failed. Returns how many were reaped.

    Never raises — it runs on the worker's schedule and a self-heal that can take
    the worker down is worse than the stuck job it is trying to clear.
    """
    own = db is None
    db = db or SessionLocal()
    reaped = 0
    try:
        now = datetime.now(timezone.utc)
        run_cutoff = now - timedelta(minutes=RUNNING_STUCK_MINUTES)
        pend_cutoff = now - timedelta(minutes=PENDING_STUCK_MINUTES)

        candidates = (db.query(IngestJob)
                      .filter(IngestJob.status.in_(("running", "pending")))
                      .all())
        for j in candidates:
            sound = _last_sound(j)
            if j.status == "running":
                if sound is not None and sound > run_cutoff:
                    continue                        # still beating — leave it
                why = (f"No progress for over {RUNNING_STUCK_MINUTES} minutes — the "
                       f"process running it was restarted or ran out of memory and "
                       f"died mid-run. Nothing was left running behind this job, so "
                       f"it is safe to start the upload again.")
            else:  # pending
                if sound is not None and sound > pend_cutoff:
                    continue                        # recently queued — give it time
                why = (f"Sat waiting for over {PENDING_STUCK_MINUTES} minutes and was "
                       f"never picked up — the process that should have run it never "
                       f"did. Start the upload again.")

            j.status = "failed"
            j.stage = "error"
            j.completed_at = now
            j.error_message = ((j.error_message or "") + " [auto-cleared: " + why + "]").strip()
            _cleanup_temp_file(j)
            reaped += 1
            log.warning("job_reaper: job %s (%s) cleared — %s", j.id, j.filename, why)

        if reaped:
            db.commit()
    except Exception:                              # noqa: BLE001
        log.exception("job_reaper: scan failed")
        try:
            db.rollback()
        except Exception:                          # noqa: BLE001
            pass
    finally:
        if own:
            db.close()
    return reaped
