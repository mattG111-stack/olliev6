"""One pipeline for every new batch — enrich, portals, re-price, dedupe, preview.

The four stages already exist and are already re-runnable and resumable; what did
not exist was anything to run them IN ORDER without a person pressing four buttons
in the right sequence and waiting between each. A manual upload stopped after the
LOAD; a scraped batch had its own chain. This drives both the same way:

    LOAD  ->  ENRICH (CoreLogic)  ->  PORTALS (HouGarden)  ->  PRICE  ->
              DEDUPE  ->  PREVIEW  ->  (a human presses Publish)

It STOPS AT PREVIEW. It never publishes: putting a batch in front of customers is
the one step that stays a person's decision, on purpose.

How it stays safe to run every worker tick:

  It advances ONE step per tick and remembers where it is on a marker row
  (IngestJob stage="auto_pipeline", the state in result_json), so a tick that
  finds a stage still running simply waits — the worker loop never blocks for the
  hours a stage can take, because each stage runs on its own thread and this only
  checks on it.

  It keys off the exact job id it started, not the stage label, so a stage that
  renames itself mid-run ("enrich (try 2)", a rate-limit message) is still
  recognised as the one it is waiting on.

  A stage that FAILS stops the pipeline and leaves it for the operator, rather
  than looping and spending money against a broken proxy.

  It runs in the WORKER process, which is why the heavy enrich/price work is here
  and not on the API's daemon thread — the split that keeps the site up.

OFF unless settings.auto_pipeline is set. Enrich and portals reach paid external
services, so the whole thing only turns on deliberately, and only once the proxy
those services need is confirmed working.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import desc
from sqlalchemy.orm import Session

from config import settings
from db import SessionLocal
from models import BatchType, ImportBatch, IngestJob

log = logging.getLogger("auto_pipeline")

# The order, and the callable that runs each step on its own thread.
STEPS = ("enrich", "portals", "price")


def enabled() -> bool:
    return bool(getattr(settings, "auto_pipeline", False))


def _staged_batch(db: Session, region: str) -> ImportBatch | None:
    from release import _staged_batch as staged
    return staged(db, BatchType.FOR_SALE.value, region)


def _marker(db: Session, batch_id: int) -> IngestJob | None:
    return (db.query(IngestJob)
            .filter(IngestJob.stage == "auto_pipeline", IngestJob.batch_id == batch_id)
            .order_by(desc(IngestJob.id)).first())


def _state(marker: IngestJob) -> dict:
    try:
        return json.loads(marker.result_json or "{}")
    except (TypeError, ValueError):
        return {}


def _save(db: Session, marker: IngestJob, state: dict, **fields) -> None:
    fields["result_json"] = json.dumps(state)
    fields.setdefault("last_progress_at", datetime.now(timezone.utc))
    db.query(IngestJob).filter(IngestJob.id == marker.id).update(fields)
    db.commit()


def _start_stage(step: str, batch_id: int, region: str, admin_id: int | None) -> int:
    """Create the stage's own tracking job and run it on a thread. Returns job id."""
    from staged_stages import create_stage_job, run_enrich_job, run_price_job

    db = SessionLocal()
    try:
        job = create_stage_job(db, stage=step, batch_id=batch_id, region=region,
                               uploaded_by_id=admin_id)
        jid = job.id
    finally:
        db.close()

    if step == "enrich":
        target = run_enrich_job
        args, kwargs = (jid, batch_id, region), {}
    elif step == "price":
        target = run_price_job
        args, kwargs = (jid, batch_id, region), {}
    else:  # portals
        from portals.runner import run_portal_job
        target = run_portal_job
        args, kwargs = (jid, batch_id, region), {"cap": 200}

    threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True,
                     name=f"auto-{step}-{batch_id}").start()
    log.warning("auto_pipeline: batch %s — started %s (job %s)", batch_id, step, jid)
    return jid


def _job_status(db: Session, job_id: int | None) -> str | None:
    if not job_id:
        return None
    row = db.query(IngestJob.status).filter(IngestJob.id == job_id).first()
    return row[0] if row else None


def _finish(db: Session, marker: IngestJob, state: dict, region: str) -> None:
    """Dedupe the new listings, move to preview, and close the marker."""
    from dedupe_forsale import dedupe_forsale_batch
    from release import send_to_preview

    batch_id = marker.batch_id
    try:
        d = dedupe_forsale_batch(db, region=region, batch_id=batch_id, dry_run=False)
        log.warning("auto_pipeline: batch %s — dedupe removed %s", batch_id, d.removed)
    except Exception:                              # noqa: BLE001
        log.exception("auto_pipeline: batch %s — dedupe failed (continuing)", batch_id)
    try:
        send_to_preview(db, region)
    except Exception:                              # noqa: BLE001
        log.exception("auto_pipeline: batch %s — send_to_preview failed", batch_id)
    state["step"] = "done"
    _save(db, marker, state, status="completed", stage="done", progress_pct=100,
          completed_at=datetime.now(timezone.utc))
    log.warning("auto_pipeline: batch %s — ready for review (preview)", batch_id)


def advance_auto_pipeline(db: Session | None = None) -> str:
    """One tick of the pipeline for the current staged batch. Never raises.

    Returns a short word for the logs/tests: 'off', 'idle', 'waiting', a step name
    it just started, 'done', or 'stopped'.
    """
    if not enabled():
        return "off"
    own = db is None
    db = db or SessionLocal()
    region = "Auckland"
    try:
        batch = _staged_batch(db, region)
        if batch is None:
            return "idle"                          # nothing staged to advance

        marker = _marker(db, batch.id)
        if marker is None:
            marker = IngestJob(batch_type=BatchType.FOR_SALE.value,
                               filename=f"auto_pipeline (batch {batch.id})",
                               status="running", stage="auto_pipeline",
                               batch_id=batch.id,
                               started_at=datetime.now(timezone.utc),
                               last_progress_at=datetime.now(timezone.utc),
                               result_json=json.dumps({"step": "enrich", "jobs": {}}))
            db.add(marker); db.commit(); db.refresh(marker)

        state = _state(marker)
        if state.get("step") == "done" or marker.status != "running":
            return "done"

        jobs = state.setdefault("jobs", {})
        step = state.get("step") or "enrich"
        admin_id = marker.uploaded_by_id

        # Is this step already under way? Check the exact job we started.
        cur_job = jobs.get(step)
        status = _job_status(db, cur_job)

        if cur_job and status == "running":
            _save(db, marker, state)               # heartbeat only
            return "waiting"

        if cur_job and status == "failed":
            _save(db, marker, state, status="failed", stage="error",
                  completed_at=datetime.now(timezone.utc),
                  error_message=(f"Stopped: the {step} stage failed. Fix it and "
                                 f"re-run the pipeline, or finish it by hand."))
            log.warning("auto_pipeline: batch %s — stopped, %s failed", batch.id, step)
            return "stopped"

        if cur_job is None:
            # Start this step.
            jobs[step] = _start_stage(step, batch.id, region, admin_id)
            _save(db, marker, state, status="running", stage=step)
            return step

        # cur_job completed -> advance to the next step, or finish.
        idx = STEPS.index(step) if step in STEPS else len(STEPS)
        if idx + 1 < len(STEPS):
            nxt = STEPS[idx + 1]
            state["step"] = nxt
            jobs[nxt] = _start_stage(nxt, batch.id, region, admin_id)
            _save(db, marker, state, status="running", stage=nxt)
            return nxt

        # Every stage done — dedupe, preview, close.
        _finish(db, marker, state, region)
        return "done"
    except Exception:                              # noqa: BLE001
        log.exception("auto_pipeline: advance failed")
        try:
            db.rollback()
        except Exception:                          # noqa: BLE001
            pass
        return "stopped"
    finally:
        if own:
            db.close()
