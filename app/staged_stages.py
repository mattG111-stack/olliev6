"""Operator-triggered stages for the staged weekly release.

The four-stage flow — LOAD → ENRICH → PRICE → PUBLISH — with each stage
independently re-runnable and its progress persisted to the database so it
survives a page refresh or a browser disconnect:

    1. LOAD     upload stages raw rows and prices them on what's present. Fast,
                no external calls. (app.ingest.ingest_for_sale, fill_missing=False)
    2. ENRICH   this module: fill blank floor / land / CV from CoreLogic, on the
                stored rows, re-runnable — a re-run only fills what is STILL blank,
                so a stage that died at 60% resumes instead of restarting.
    3. PRICE    this module: re-run the pricing pipeline over the staged batch
                (app.reprice.reprice_batch), so a fix to the pricing code re-values
                the batch without a re-upload.
    4. PUBLISH  app.release.publish_release promotes the staged batch to live.

Each ENRICH / PRICE run is tracked by an IngestJob row: rows_total / rows_inserted
(processed) / rows_filled / rows_missed and a terminal status, all committed as the
stage runs. The long-running work happens on a background thread that owns its own
DB session, so the request returns immediately and /health keeps answering while
the stage runs (the container is no longer killed for a blocked request process).
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta, timezone

import json

from sqlalchemy import func
from sqlalchemy.orm import Session

from .db import SessionLocal
from .ingest import _blank, _needs_lookup
from .models import BatchType, ImportBatch, IngestJob, PropertyForSale
from .propertyvalue import PV_BLOCKED, PV_ERROR, PV_OK, pv_lookup_status
from .prior_price import NEGOTIATION_DISCOUNT_PCT, ROUND_TO, carry_forward_prices
from .runlog import record as _record
from .release import WORKING_STATUSES, hold_flagged_rows
from .reprice import reprice_batch


# A scraped CV that disagrees with CoreLogic's by more than this is treated as
# stale/wrong and corrected. CV *is* the council rating value and CoreLogic sources
# it directly, so a real disagreement means the scrape captured the wrong figure
# (6 Cassino Terrace: scraped $500k, real RV $1.25M). 10% ignores rounding noise.
_CV_CORRECT_TOL = 0.10


def corrected_cv(scraped, corelogic, tol: float = _CV_CORRECT_TOL):
    """CoreLogic's capital value is the authoritative council rating value. Return
    it when it materially disagrees with a present scraped CV — the scrape is then
    stale or wrong and CoreLogic overrides it. Returns None (keep the scraped CV)
    when they agree, or when either side is missing. A blank scraped CV is filled
    by the ordinary fill-pairs path, not here."""
    try:
        cl = float(corelogic)
        sc = float(scraped)
    except (TypeError, ValueError):
        return None
    if cl <= 0 or sc <= 0:
        return None
    if abs(sc - cl) > tol * cl:
        return cl
    return None


def _apply_pv_record(p: PropertyForSale, pv: dict) -> None:
    """Store CoreLogic's own cached fields on the row: its AVM range, its CV, and
    the last recorded sale (price + date). These are refreshed to the latest lookup
    — unlike the pricing ATTRIBUTES (floor/land/CV/beds/baths/zoning), which only
    fill blanks and must never overwrite a real scraped value. pv_estimate_mid also
    feeds the pricing engine's CV-fallback anchor when a council CV is missing."""
    p.pv_estimate_low = pv.get("estimate_low")
    p.pv_estimate_high = pv.get("estimate_high")
    p.pv_estimate_mid = pv.get("estimate_mid")
    p.pv_cv = pv.get("cv")
    p.pv_url = pv.get("url")
    p.pv_last_sale_price = pv.get("last_sale_price")
    p.pv_last_sale_date = pv.get("last_sale_date")
    try:
        p.pv_data = json.dumps(pv)
    except (TypeError, ValueError):
        pass

# CoreLogic record key → our PropertyForSale attribute. Mirrors the pairs in
# ingest._fill_df_from_corelogic, but writing to the stored row's attributes.
_FILL_PAIRS = (
    ("floor_area_m2", "floor_area_m2"),
    ("land_area_m2", "land_area_m2"),
    ("beds", "beds"),
    ("baths", "baths"),
    ("cv_numeric", "cv"),
    ("zoning", "zoning"),
)


def _staged_forsale_batch(db: Session, region: str) -> ImportBatch | None:
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                    ImportBatch.region == region,
                    ImportBatch.status.in_(WORKING_STATUSES))
            .order_by(ImportBatch.id.desc()).first())


def enrichable_forsale_batch(db: Session, region: str) -> ImportBatch | None:
    """The batch CoreLogic should be run against: staged if there is one, else live.

    Enrich used to accept ONLY a staged batch, and publishing flips a batch out
    of that status. So the moment a load went live it became impossible to
    bulk-enrich — the endpoint answered "No staged for-sale batch to enrich" and
    the only remaining option was the per-listing button, one property at a
    time, on a batch of eleven thousand.

    That is backwards. A published batch with thousands of rows still missing a
    floor area or a CV is exactly the batch that needs the lookup most: those
    rows are held, unpriced, and sitting in front of customers as gaps.

    Staged still wins when both exist — a staged batch is the one about to
    become live, and improving it before publish is better than after.
    """
    staged = _staged_forsale_batch(db, region)
    if staged is not None:
        return staged
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                    ImportBatch.region == region,
                    ImportBatch.is_active.is_(True))
            .order_by(ImportBatch.id.desc()).first())


def _row_input(p: PropertyForSale) -> dict:
    """The subset of scrape-shaped fields _needs_lookup reads from a stored row."""
    return {
        "key_floor_area": p.floor_area_m2,
        "key_land_area": p.land_area_m2,
        "cv_numeric": p.cv_numeric,
        "address": p.address,
    }


def create_stage_job(db: Session, *, stage: str, batch_id: int | None,
                     region: str, uploaded_by_id: int | None) -> IngestJob:
    """Create the IngestJob that tracks a background stage and return it.

    batch_id may be None: a portal sweep belongs to no batch, because it is
    looking for properties that are not in one yet. The column is a nullable
    foreign key, so None stores cleanly where 0 would break the constraint.
    """
    job = IngestJob(
        batch_type=BatchType.FOR_SALE.value,
        filename=(f"{stage} (batch {batch_id})" if batch_id else stage),
        status="pending",
        stage=stage,
        batch_id=batch_id,
        uploaded_by_id=uploaded_by_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _update(db: Session, job_id: int, **kwargs) -> None:
    # Trim any text to what its column holds, here rather than at each call
    # site. Postgres refuses an overlong value rather than truncating it, so a
    # status line one character too long does not produce a shortened status
    # line — it fails the write and the stage reports as broken. That has
    # already happened once, which is why one caller passes stage=_stage[:64];
    # doing it here means the next caller does not have to know.
    for k, v in list(kwargs.items()):
        if isinstance(v, str):
            col = IngestJob.__table__.columns.get(k)
            n = getattr(getattr(col, "type", None), "length", None)
            if n and len(v) > n:
                kwargs[k] = v[:n]
    # Every write is a heartbeat. Doing it here rather than at each call site
    # means no future progress update can forget to say "still alive" — which
    # matters, because a missed heartbeat now reads as a dead job.
    kwargs.setdefault("last_progress_at", datetime.now(timezone.utc))
    db.query(IngestJob).filter(IngestJob.id == job_id).update(kwargs)
    db.commit()


def job_was_cancelled(db: Session, job_id: int) -> bool:
    """Has someone taken this job off us while it was running?

    Restart marks the old job abandoned and starts a new one. The old thread
    cannot be killed from outside — Python has no safe way to stop a thread —
    so it has to agree to stop, and this is where it asks. Without it, a forced
    restart would leave the previous worker running underneath the new one:
    two enrichers on one batch, both spending lookups, in a container sized for
    one.
    """
    row = (db.query(IngestJob.status)
           .filter(IngestJob.id == job_id).first())
    return row is not None and row[0] != "running"


def _sleep_alive(db: Session, job_id: int, seconds: float, *,
                 label: str | None = None) -> bool:
    """Wait, without going quiet and without ignoring a restart.

    Returns False if the job was cancelled during the wait, True if it slept it
    out. A long wait — the rate-limit ladder goes to five minutes, and the
    retry ladder to fifteen — must not look like a hang, so it beats every
    thirty seconds and checks whether someone has restarted it.
    """
    if label:
        _update(db, job_id, stage=label[:64])
    waited = 0.0
    while waited < seconds:
        slice_ = min(30.0, seconds - waited)
        time.sleep(slice_)
        waited += slice_
        if job_was_cancelled(db, job_id):
            return False
        _update(db, job_id)                    # heartbeat only
    return True


# ---- shared guard -----------------------------------------------------------
# A stage that has not made a sound in this long is treated as DEAD — the
# container was killed or redeployed mid-run, so its job row is stuck at
# "running" and would otherwise block the button forever.
#
# MEASURED FROM THE LAST HEARTBEAT, NOT FROM THE START. It used to be twenty
# minutes from started_at, which is a stopwatch on the job rather than a check
# on its pulse: an enrich over a full batch runs for hours, so twenty minutes
# in, a perfectly healthy run was declared abandoned. That is the red error
# that appeared on a screen whose run was still going, and it also took the
# lock off, so pressing the button again started a SECOND enrich on top of the
# first — two heavy workers in a container sized for one, which is how it gets
# OOM-killed and how "it timed out" becomes "and now it will not restart".
#
# Ten minutes because the longest legitimate silence is shorter than that: the
# rate-limit ladder waits at most five minutes and beats every thirty seconds
# while it does, and the lookup loop beats every five addresses.
STAGE_NO_PROGRESS_MINUTES = 10

# Only for rows written before heartbeats existed, which have no
# last_progress_at to read. Generous, because for those the old stopwatch is
# all there is.
STAGE_STALE_MINUTES = 20


def _aware(dt: datetime | None) -> datetime | None:
    """A timestamp that can be compared, whatever the database handed back.

    Postgres TIMESTAMPTZ round-trips with a timezone; SQLite drops it and
    returns a naive datetime. Comparing the two raises TypeError, and this
    comparison sits inside the check that guards the Enrich button — so an
    unguarded one turns "is a run in flight" into a 500 rather than an answer.
    Anything naive coming out of the database is UTC, because everything going
    in is.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _job_is_alive(j: IngestJob, now: datetime) -> bool:
    """Has this job made a sound recently enough to still be believed?"""
    beat = _aware(getattr(j, "last_progress_at", None))
    if beat is not None:
        return beat > now - timedelta(minutes=STAGE_NO_PROGRESS_MINUTES)
    # No heartbeat recorded: an older row, or one that died before its first
    # update. Fall back to the start time.
    started = _aware(j.started_at)
    return started is not None and started > now - timedelta(
        minutes=STAGE_STALE_MINUTES)


def stage_running(db: Session, batch_id: int | None, stage: str) -> bool:
    """True if this stage is genuinely running.

    Prevents a second (heavy) background worker from stacking on the first — the
    main cause of an out-of-memory container kill, and for anything that calls a
    paid service, a second worker is a second bill for the same rows. A job that
    has stopped beating past STAGE_NO_PROGRESS_MINUTES (its container died) is
    auto-cleared to "failed" so the operator can start again, instead of being
    locked out for ever by a run nobody can see.

    `batch_id=None` asks about a stage that belongs to no batch — filling the
    gaps on pending portal listings, for instance, which are not in one yet.
    Comparing a column to None with == is not the same question as IS NULL in
    every dialect, so that case is spelled out rather than left to luck.
    """
    q = db.query(IngestJob).filter(IngestJob.stage == stage,
                                   IngestJob.status == "running")
    q = q.filter(IngestJob.batch_id.is_(None) if batch_id is None
                 else IngestJob.batch_id == batch_id)
    jobs = q.all()
    if not jobs:
        return False
    now = datetime.now(timezone.utc)
    if any(_job_is_alive(j, now) for j in jobs):
        return True
    # Every "running" job for this stage is silent — release the lock.
    for j in jobs:
        j.status = "failed"
        j.stage = "error"
        j.error_message = ((j.error_message or "")
                           + " [auto-cleared: run abandoned / container restarted]").strip()
        j.completed_at = now
    db.commit()
    return False


def abandon_stage(db: Session, batch_id: int, stage: str) -> int:
    """Take a stage off whoever is running it, so a restart can take over.

    Returns how many jobs were released. The thread behind an abandoned job
    stops at its next checkpoint (see job_was_cancelled) rather than being
    killed, so a restart cannot leave two workers on one batch.

    This is what makes the Restart button honest. Without it, restarting during
    a run that is merely slow would stack a second worker on the first; with
    it, the old one stands down.
    """
    jobs = (db.query(IngestJob)
            .filter(IngestJob.batch_id == batch_id,
                    IngestJob.stage == stage,
                    IngestJob.status == "running")
            .all())
    now = datetime.now(timezone.utc)
    for j in jobs:
        j.status = "failed"
        j.stage = "stopped"
        j.error_message = ((j.error_message or "")
                           + " [stopped by a restart]").strip()
        j.completed_at = now
    db.commit()
    return len(jobs)


# ---- ENRICH -----------------------------------------------------------------
# How long to wait after a rate limit before trying the same address again.
# Doubling-ish rather than fixed: a short pause clears a burst limit, a long one
# clears a per-minute quota, and going straight to five minutes wastes an hour
# on a block that would have cleared in five seconds.
BLOCK_BACKOFF = (5, 15, 45, 120, 300)

# Once told off, stay slower. A pace that earned a rate limit will earn another.
PACE_BACKOFF = 2.0
MAX_DELAY = 8.0

# Blocks that survive the entire backoff ladder, on this many rows in a row,
# are an IP block rather than a rate limit and no amount of waiting fixes them.
HARD_BLOCK_ROWS = 3


# How long to wait before trying again, after a stop that we expect to clear on
# its own. A rate limit clears in a minute; an IP block or a dropped connection
# takes longer; nothing that is still broken after fifteen minutes is going to
# be fixed by a sixteenth.
#
# These exist because the two ways this run stops early — refused by CoreLogic,
# or unable to reach it at all — are both TEMPORARY, and both used to end the
# run with a red message that sat there until somebody noticed and pressed the
# button. Overnight that is eight hours of nothing. The run now waits and picks
# itself back up, and because it resumes rather than restarts, a retry costs
# only the addresses it has not already answered.
AUTO_RETRY_WAITS = (60, 300, 900)          # 1 min, 5 min, 15 min


def _enrich_pass(db: Session, job_id: int, batch_id: int, region: str,
                 *, delay: float, cap: int, attempt: int,
                 retries_left: int) -> str:
    """One attempt at enriching the batch. Returns what happened:

        "done"       finished the work list
        "retry"      stopped on something temporary — worth trying again
        "stop"       stopped on something that will not fix itself
        "cancelled"  someone restarted it; this thread must stand down

    Fills blank floor / land / CV on the batch's rows from CoreLogic. Resumable
    by design — only rows still missing a pricing-critical field and not yet
    answered are looked up, so a second attempt continues rather than repeats.

    Memory-flat by design: it never materialises the whole batch as ORM objects.
    A lightweight scan (id + the three fields that decide "needs a lookup") builds
    the work list, then each row is fetched, filled and released one at a time,
    with the identity map cleared every 25 lookups. An 11k-row batch stays flat in
    memory instead of holding 11k objects for 30 minutes — which is what let a few
    stacked runs OOM-kill the container."""
    try:
        _update(db, job_id, status="running",
                stage="enrich" if attempt == 1 else f"enrich (try {attempt})",
                started_at=datetime.now(timezone.utc), progress_pct=0,
                rows_filled=0, rows_missed=0)

        # Re-apply holds on the CURRENT pricing before choosing what to enrich, so
        # the below-margin skip works even when the batch was loaded by an older
        # build (its rows would still be is_held=False) or the margin threshold has
        # since changed. Deploying new code does not retroactively re-hold already-
        # loaded rows — this does. Cheap: no external calls, just a margin re-check.
        hold_flagged_rows(db, batch_id)
        db.expunge_all()  # release the ORM objects it loaded — keep memory flat

        # Lightweight scan — tuples, not ORM objects.
        scan = (db.query(PropertyForSale.id, PropertyForSale.floor_area_m2,
                         PropertyForSale.land_area_m2, PropertyForSale.cv_numeric,
                         PropertyForSale.asking_price, PropertyForSale.pv_checked_at,
                         PropertyForSale.is_held, PropertyForSale.hold_reason,
                         PropertyForSale.pv_last_sale_price,
                         PropertyForSale.valuation_last_sold_value,
                         PropertyForSale.address, PropertyForSale.suburb)
                .filter(PropertyForSale.import_batch_id == batch_id)
                .order_by(PropertyForSale.id).all())

        def _cv_suspect(cv, asking) -> bool:
            # The scraper fills a by-negotiation asking from the CV, so cv == asking
            # to the dollar flags a CV that may be the copied/wrong figure (6 Cassino
            # Terrace). Look these up so CoreLogic can confirm or correct the CV even
            # when floor/land are already present.
            return bool(cv and asking and abs(cv - asking) < 0.005 * cv)

        # Bulk enrich runs ONLY on the deal candidates: held rows (below the margin
        # floor after the first load-pricing, or otherwise flagged) are skipped, so
        # CoreLogic is spent on the ~1-in-5 keepers instead of the whole file — far
        # fewer lookups, far less likely to hit the rate-limit block. A held row can
        # still be enriched on demand via its per-listing button.
        # Also skip rows CoreLogic has already answered (pv_checked_at set — stamped
        # only on a real answer, never on a block), so each re-run RESUMES on
        # genuinely-unvisited keepers rather than re-hitting ones we already have.
        # A hold means one of two completely different things and this used to
        # treat them the same, which is what emptied the feed.
        #
        # Skipping a row held for "no deal here" is right: the data is fine and
        # a paid lookup buys nothing. Skipping a row held for "Missing floor
        # area" is exactly backwards — CoreLogic is the thing that would fill
        # it. That row was never looked up, so the floor area was never filled,
        # so it stayed held and UNPRICED permanently. The rows that most needed
        # the lookup were the only ones guaranteed not to get it, and every
        # weekly load added more of them.
        #
        # It also explains a run that reports a hundred lookups on an eleven
        # thousand row batch: after the first pricing pass most rows are held,
        # and only the unheld remainder was ever eligible.
        from .release import hold_is_a_data_gap

        def _eligible(r) -> bool:
            if not r.is_held:
                return True
            return hold_is_a_data_gap(r.hold_reason)

        def _no_sale_history(r) -> bool:
            """We have never seen what this property last sold for.

            This is why a run reported "147/147 looked up" on a 2,141-row load
            and looked like it had given up early. It had not — it asked about
            every row it thought needed asking about, and the test was "is a
            floor area, land area or CV missing". Nearly every row already has
            those, so nearly every row was skipped.

            But the same lookup also returns the LAST SALE, and nothing was ever
            asking for it. `Last sold` was empty on all 2,141 rows of the export
            and would have stayed empty for ever, because a row with a floor
            area was never eligible to be asked. A previous sale price is not a
            nice-to-have: it is shown on the listing, it is how a scraped
            "price" that is really the last sale gets caught, and it is the only
            independent read on a property whose council record is stale.
            """
            return _blank(r.pv_last_sale_price) and _blank(r.valuation_last_sold_value)

        todo = [(r.id, r.address, r.suburb) for r in scan
                if _eligible(r)
                and _blank(r.pv_checked_at)
                and (_blank(r.floor_area_m2) or _blank(r.land_area_m2)
                     or _blank(r.cv_numeric) or _cv_suspect(r.cv_numeric, r.asking_price)
                     or _no_sale_history(r))
                and not _blank(r.address)]
        _from_held = sum(1 for r in scan if r.is_held
                         and hold_is_a_data_gap(r.hold_reason)
                         and _blank(r.pv_checked_at) and not _blank(r.address)
                         and (_blank(r.floor_area_m2) or _blank(r.land_area_m2)
                              or _blank(r.cv_numeric)
                              or _cv_suspect(r.cv_numeric, r.asking_price)))
        # Say up front how many of the batch this run will ask about, and how
        # many it is deliberately leaving alone. "147/147 looked up" on a
        # 2,141-row load reads as a run that gave up a fourteenth of the way in.
        # It was not — it asked about everything it had a reason to ask about
        # and finished. That distinction only becomes visible if the total the
        # run is measured against is stated next to the size of the batch.
        _batch_rows = len(scan)
        _already = sum(1 for r in scan if not _blank(r.pv_checked_at))
        print(f"  [enrich] {len(todo)} of {_batch_rows:,} rows to look up "
              f"({_from_held} held for a data gap a lookup can fill; "
              f"{_already:,} already answered by an earlier run)")
        _record(db, stage="enrich", event="lookups_planned", batch_id=batch_id,
                job_id=job_id, count=len(todo),
                detail=(f"{len(todo):,} of {_batch_rows:,} listings will be looked "
                        f"up — the rest already hold everything a lookup would "
                        f"fill ({_already:,} answered on an earlier run)"))
        del scan
        need = len(todo)
        _update(db, job_id, rows_total=need, rows_inserted=0)

        looked = filled = misses = consec_fail = corrected = 0
        blocked = consec_block = errors = 0
        for pid, address, suburb in todo:
            if looked >= cap:
                break
            # Say "still here" often enough that no legitimate gap between
            # beats can be mistaken for a dead run, and notice a restart
            # promptly rather than at the end of the batch. Every five rather
            # than every row: one small UPDATE per five lookups is nothing
            # beside the lookups themselves, and a single slow row can take
            # twenty seconds to time out.
            if looked % 5 == 0:
                if job_was_cancelled(db, job_id):
                    return "cancelled"
                _update(db, job_id)
            q = ", ".join(x for x in (str(address), str(suburb or "").strip(), "Auckland")
                          if x and x.lower() != "nan")
            looked += 1
            try:
                pv, status = pv_lookup_status(q)
            except Exception:
                pv, status = None, "error"

            # A 401/403/429 from CoreLogic means we are being REFUSED, not that
            # it lacks the address — so it is handled completely differently
            # from an empty result. Empty could be forty unknown addresses;
            # refused is unambiguous.
            if status == PV_BLOCKED:
                # A rate limit is not a failure. It is CoreLogic asking us to
                # slow down, and the right answer is to slow down.
                #
                # This used to abandon the whole run after five straight blocks
                # and leave a message telling somebody to wait and re-run by
                # hand. On a batch of eleven thousand a rate limit is not an
                # exceptional event, it is the expected one — so the job died
                # part-way through almost every time, and "CoreLogic did a
                # hundred houses" is exactly what that looks like from outside.
                #
                # Now it backs off and retries the SAME address, waiting longer
                # each time, and permanently slows the pacing for the rest of
                # the run once it has been told off. Only a block that survives
                # the whole backoff ladder, three rows running, is treated as a
                # real IP ban rather than a rate limit.
                blocked += 1
                got_through = False
                for wait in BLOCK_BACKOFF:
                    # Beats while it waits and notices a restart, so a
                    # five-minute pause is not mistaken for a hang.
                    if not _sleep_alive(db, job_id, wait,
                                        label=f"rate-limited, waiting {wait}s"):
                        return "cancelled"
                    try:
                        pv, status = pv_lookup_status(q)
                    except Exception:
                        pv, status = None, "error"
                    if status != PV_BLOCKED:
                        got_through = True
                        break
                # Whatever happens, go slower from here on. A rate limit that
                # was hit once will be hit again at the same pace.
                delay = min(delay * PACE_BACKOFF, MAX_DELAY)
                if not got_through:
                    consec_block += 1
                    if consec_block >= HARD_BLOCK_ROWS:
                        db.commit()
                        _stop_msg = (
                            f"BLOCKED by CoreLogic at lookup {looked}/{need}, "
                            f"and it did not clear after backing off up to "
                            f"{BLOCK_BACKOFF[-1]}s on {consec_block} rows in a "
                            f"row. "
                            f"That is an IP block rather than a rate limit. "
                            f"Everything looked up so far is saved, so "
                            f"a re-run resumes from here.")
                        # An IP block lifts on its own. Keep the job alive and
                        # let the caller wait it out rather than ending the run
                        # and waiting for a person to press the button.
                        if retries_left > 0:
                            _update(db, job_id, rows_inserted=looked,
                                    rows_filled=filled, rows_missed=misses,
                                    error_message=_stop_msg)
                            return "retry"
                        _update(db, job_id, status="failed", stage="error",
                                rows_inserted=looked, rows_filled=filled,
                                rows_missed=misses, error_message=_stop_msg,
                                completed_at=datetime.now(timezone.utc))
                        return "stop"
                    continue                       # move on, keep the run alive
                consec_block = 0
                # Fell through the backoff and got an answer — carry on below
                # with the retry's result rather than the blocked one.
            else:
                consec_block = 0

            p = db.get(PropertyForSale, pid)
            if p is not None:
                # Stamp every row we got a real answer for (hit OR miss) so the
                # review grid can tell a row CoreLogic never reached ("Not
                # enriched") from one it reached but had nothing for ("CoreLogic
                # missed") — and so re-runs skip it. A BLOCK is NOT an answer, so
                # leave those unstamped: they stay in `todo` for the next wave.
                # A TRANSPORT FAILURE IS NOT AN ANSWER EITHER. This used to
                # stamp on anything that wasn't a 401/403/429, so a run that
                # could not reach CoreLogic at all — DNS, proxy, TLS, timeout,
                # every request failing identically — marked all 2,141 rows
                # "checked" and filled nothing. Every re-run after that found an
                # empty work list and completed instantly, because `todo` skips
                # stamped rows. The batch was then permanently un-enrichable and
                # the screen said "2,141/2,141 looked up · 0 filled", which reads
                # as "CoreLogic has nothing for these addresses" and is the exact
                # opposite of what happened.
                if status not in (PV_BLOCKED, PV_ERROR):
                    p.pv_checked_at = datetime.now(timezone.utc)
                if pv:
                    # Fill blank pricing attributes (never overwrite real values)...
                    for our_attr, pv_key in _FILL_PAIRS:
                        if _blank(getattr(p, our_attr, None)) and pv.get(pv_key):
                            setattr(p, our_attr, pv.get(pv_key))
                            filled += 1
                    # ...correct a present-but-wrong CV from CoreLogic's authoritative
                    # rating value (the one exception to "never overwrite": CV *is*
                    # the council RV, and a scraped CV that disagrees is wrong)...
                    new_cv = corrected_cv(p.cv_numeric, pv.get("cv"))
                    if new_cv is not None:
                        p.cv_numeric = new_cv
                        corrected += 1
                    # ...and capture CoreLogic's AVM / CV / last-sold on every hit.
                    _apply_pv_record(p, pv)

            if not pv:
                misses += 1
                # An address CoreLogic doesn't hold and a request that never got
                # there are both "no record" to this loop, and they need opposite
                # responses: one is a data limit you accept, the other is an
                # outage you fix and re-run. Counted apart so the summary can say
                # which happened instead of blaming the addresses.
                if status == PV_ERROR:
                    errors += 1
                consec_fail += 1
                # Circuit breaker: 40 misses in a row means CoreLogic is refusing
                # (rate-limited), not that these addresses are genuinely unknown.
                if consec_fail >= 40:
                    db.commit()
                    _unreachable = errors >= consec_fail
                    _stop_msg = (
                        f"stopped after {consec_fail} lookups in a row came "
                        f"back with nothing, at lookup {looked}/{need}. "
                        + (f"All {errors} failed before reaching CoreLogic "
                           f"(network, DNS or proxy) — check outbound access, "
                           f"nothing is wrong with the addresses. "
                           if _unreachable else
                           "CoreLogic is likely unreachable or rate-limiting. ")
                        + "Nothing was marked checked, so a re-run retries "
                          "every one of them.")
                    # Unreachable is a temporary condition — a dropped
                    # connection, a proxy blip, a rate limit. Waiting is the
                    # right answer, and it is the answer a person would give if
                    # they were sitting here watching it.
                    if retries_left > 0:
                        _update(db, job_id, rows_inserted=looked,
                                rows_filled=filled, rows_missed=misses,
                                error_message=_stop_msg)
                        return "retry"
                    _update(db, job_id, status="failed", stage="error",
                            rows_inserted=looked, rows_filled=filled, rows_missed=misses,
                            error_message=_stop_msg,
                            completed_at=datetime.now(timezone.utc))
                    return "stop"
            else:
                consec_fail = 0

            if looked % 25 == 0:
                db.commit()               # persist this chunk's fills durably
                db.expunge_all()          # release the identity map — keeps memory flat
                pct = int(100 * looked / need) if need else 100
                _update(db, job_id, rows_inserted=looked, rows_filled=filled,
                        rows_missed=misses, progress_pct=min(pct, 99))
            time.sleep(delay)

        db.commit()
        if corrected:
            print(f"[enrich] corrected {corrected} wrong CV(s) from CoreLogic's "
                  f"rating value", flush=True)
        # Distinguish genuine "no record" from transport blocks in the summary, so
        # the operator can tell "CoreLogic just doesn't have these" from "CoreLogic
        # refused us". `blocked` counts 401/403/429; a nonzero value with few fills
        # means you're being rate-limited even if the run didn't hit the fast-stop.
        not_found = misses - blocked - errors
        summary = (f"done: {filled} field(s) filled, {corrected} CV(s) corrected, "
                   f"{not_found} address(es) not in CoreLogic, {blocked} blocked "
                   f"(HTTP 401/403/429), {errors} never reached it")
        print(f"[enrich] {summary}", flush=True)
        # The summary went to STDOUT only, and the job row got stage="done" — so
        # the page read "done: done · 2,141/2,141 looked up · 0 filled · 0
        # missed" and never mentioned that 1,994 of those lookups were REFUSED.
        # Filled-nothing and refused-everything look identical that way, and
        # they need opposite responses: one is a data problem, the other is a
        # rate limit that clears on its own.
        #
        # IngestJob.stage is String(64) — a longer value used to be silently
        # truncated by the database — so this is written short on purpose.
        _stage = (f"{filled} filled · {blocked} blocked · {not_found} not found"
                  + (f" · {errors} unreachable" if errors else ""))
        _record(db, stage="enrich", event="lookups_done", batch_id=batch_id,
                job_id=job_id, count=looked, level="warn" if errors or blocked else "info",
                detail=(f"{looked:,} addresses looked up: {filled:,} blank fields "
                        f"filled, {corrected:,} council valuations corrected, "
                        f"{not_found:,} not held by the data provider, "
                        f"{blocked:,} refused (rate limit), "
                        f"{errors:,} never reached it"))
        if errors:
            _record(db, stage="enrich", event="lookups_unreachable", batch_id=batch_id,
                    job_id=job_id, count=errors, level="warn",
                    detail=(f"{errors:,} lookups failed before reaching the provider "
                            f"(network, DNS, proxy or timeout). Those rows were NOT "
                            f"marked as checked, so re-running retries every one."))
        _update(db, job_id, status="completed", stage=_stage[:64], progress_pct=100,
                rows_inserted=looked, rows_filled=filled, rows_missed=misses,
                error_message=(
                    (f"{errors} lookup(s) never reached CoreLogic (network, DNS, proxy "
                     f"or timeout). Those rows were NOT marked checked, so re-running "
                     f"retries every one. " if errors else "")
                    + (f"BLOCKED on {blocked} lookup(s) (HTTP 401/403/429) — CoreLogic "
                       f"is rate-limiting; those rows kept their scraped values. Re-run "
                       f"later to fill them." if blocked else "")
                ) or None,
                completed_at=datetime.now(timezone.utc))
        return "done"
    except Exception as e:
        _update(db, job_id, status="failed", stage="error",
                error_message=f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:2000]}",
                completed_at=datetime.now(timezone.utc))
        return "stop"


def run_enrich_job(job_id: int, batch_id: int, region: str,
                   *, delay: float = 0.5, cap: int = 20000,
                   retry_waits: tuple[int, ...] = AUTO_RETRY_WAITS) -> str:
    """Enrich the batch, and keep trying if it stops on something temporary.

    Returns the final outcome ("done" / "stop" / "cancelled"), which is what the
    tests assert on; nothing in the app reads it, because progress lives in the
    job row.

    The two ways this run stops early — refused by CoreLogic, or unable to reach
    it at all — are both conditions that pass. Until now either one ended the
    run and left a red message waiting for somebody to press the button again,
    which overnight means the enrich simply does not happen. Now it waits and
    picks up where it left off, up to three times, over about twenty minutes.

    Retrying is only safe because the pass RESUMES: every address already
    answered is stamped and skipped, so a second attempt asks about what is
    left, not about the whole batch again. A retry loop over a non-resumable job
    would just spend the same lookups repeatedly and never finish.
    """
    db = SessionLocal()
    try:
        attempt = 0
        while True:
            retries_left = len(retry_waits) - attempt
            outcome = _enrich_pass(db, job_id, batch_id, region,
                                   delay=delay, cap=cap, attempt=attempt + 1,
                                   retries_left=retries_left)
            if outcome != "retry":
                return outcome
            wait = retry_waits[attempt]
            attempt += 1
            _record(db, stage="enrich", event="auto_retry", batch_id=batch_id,
                    job_id=job_id, count=attempt, level="warn",
                    detail=(f"stopped on something temporary; waiting "
                            f"{wait // 60 or 1} minute(s) and resuming "
                            f"(try {attempt + 1} of {len(retry_waits) + 1})"))
            mins = max(1, wait // 60)
            if not _sleep_alive(db, job_id, wait,
                                label=f"waiting {mins}m, then try {attempt + 1}"):
                return "cancelled"
    except Exception as e:
        _update(db, job_id, status="failed", stage="error",
                error_message=f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:2000]}",
                completed_at=datetime.now(timezone.utc))
        return "stop"
    finally:
        db.close()


def enrich_one(db: Session, listing_id: int) -> tuple[PropertyForSale | None, str]:
    """CoreLogic-enrich a SINGLE listing on demand and commit. Fills blank floor /
    land / CV, corrects a wrong CV, captures CoreLogic's AVM/CV/last-sold, and
    stamps pv_checked_at (unless blocked). Synchronous — one lookup is quick.
    Returns (row, status); status is PV_OK / 'missed' / PV_BLOCKED / 'error' /
    'not_found'. Powers the per-listing 'Enrich (CoreLogic)' button."""
    p = db.get(PropertyForSale, listing_id)
    if p is None:
        return None, "not_found"
    q = ", ".join(x for x in (str(p.address), str(p.suburb or "").strip(), "Auckland")
                  if x and x.lower() != "nan")
    try:
        pv, status = pv_lookup_status(q)
    except Exception:
        pv, status = None, "error"
    if status != PV_BLOCKED:
        p.pv_checked_at = datetime.now(timezone.utc)
    if pv:
        for our_attr, pv_key in _FILL_PAIRS:
            if _blank(getattr(p, our_attr, None)) and pv.get(pv_key):
                setattr(p, our_attr, pv.get(pv_key))
        new_cv = corrected_cv(p.cv_numeric, pv.get("cv"))
        if new_cv is not None:
            p.cv_numeric = new_cv
        _apply_pv_record(p, pv)
    elif status not in (PV_BLOCKED, "error"):
        status = "missed"
    db.commit()
    db.refresh(p)
    return p, status


# ---- PRICE ------------------------------------------------------------------
def run_price_job(job_id: int, batch_id: int, region: str) -> None:
    """Background worker: re-run the pricing pipeline over the staged batch using
    its CURRENT stored attributes (i.e. after enrich), committing the new
    valuations. Then re-apply the pre-publish holds so the review reflects the
    fresh numbers. Re-runnable."""
    db = SessionLocal()
    try:
        _update(db, job_id, status="running", stage="price",
                started_at=datetime.now(timezone.utc), progress_pct=10)
        # Before pricing, not after: a listing whose vendor has withdrawn the
        # price needs their last advertised one ON the row for the pipeline to
        # work from, and doing it afterwards would price the listing blind and
        # then stamp a number it never used.
        _update(db, job_id, stage="carrying prices forward")
        carried = carry_forward_prices(db, batch_id, region)
        if carried:
            _record(db, stage="price", event="prices_carried_forward",
                    batch_id=batch_id, job_id=job_id, count=carried,
                    detail=(f"{carried:,} listings now show no advertised price but "
                            f"were advertised in an earlier load — priced from the "
                            f"vendor's own last figure, less "
                            f"{NEGOTIATION_DISCOUNT_PCT:.0%}, rounded to the nearest "
                            f"${int(ROUND_TO):,}"))
        # Re-pricing a full batch is one long call. Beat as it goes, or the
        # liveness check reads several silent minutes as a hung process and
        # releases the lock while the run is still working — which is the fault
        # this whole change exists to fix, and it would be careless to fix it
        # for enrich and leave it here.
        def _beat(done: int, total: int) -> None:
            _update(db, job_id, stage="price",
                    rows_total=total, rows_inserted=done,
                    progress_pct=min(10 + int(70 * done / total) if total else 10, 80))

        res = reprice_batch(db, batch_id, region=region, commit=True,
                            on_chunk=_beat)
        if res.error:
            raise RuntimeError(res.error)
        _update(db, job_id, stage="price", progress_pct=85, rows_total=res.rows,
                rows_inserted=res.rows)
        held = hold_flagged_rows(db, batch_id)
        # Record what the run produced, not just that it finished. A pricing run
        # that priced everything and flagged nothing, and one that fell over
        # halfway, both used to leave the same word on the screen: "done".
        from .deal_funnel import deal_funnel
        f = deal_funnel(db, batch_id)
        _record(db, stage="price", event="priced", batch_id=batch_id, job_id=job_id,
                count=res.rows,
                detail=(f"{res.rows:,} listings priced, {held:,} held back from the "
                        f"customer feed, {f.flagged:,} flagged as deals"))
        for step in f.steps:
            if step.lost:
                _record(db, stage="price", event="deals_lost_at_step",
                        batch_id=batch_id, job_id=job_id, count=step.lost,
                        commit=False,
                        detail=f"{step.lost:,} dropped at \u201c{step.label}\u201d — {step.why}")
        if f.mismatch:
            _record(db, stage="price", event="flag_disagrees_with_figures",
                    batch_id=batch_id, job_id=job_id, count=f.mismatch, level="warn",
                    commit=False,
                    detail=(f"{f.mismatch:,} listings pass every deal test and are "
                            f"still not flagged — the flag and the figures beside it "
                            f"were written by different runs"))
        db.commit()
        _update(db, job_id, status="completed", stage="done", progress_pct=100,
                rows_rejected=held, completed_at=datetime.now(timezone.utc))
    except Exception as e:
        _update(db, job_id, status="failed", stage="error",
                error_message=f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:2000]}",
                completed_at=datetime.now(timezone.utc))
    finally:
        db.close()


# ---- self-heal: re-price stale AVM-inflated valuations on deploy -------------
from sqlalchemy import func  # noqa: E402


def _has_stale_avm_rows(db: Session, batch_id: int) -> bool:
    """Cheap existence check for the pre-guard pricing bug's fingerprint.

    The old anchor guard set a CV-anchored valuation UP to ~95% of the external
    AVM (CoreLogic/homes) whenever that AVM was high — surfacing a $70k section
    at $1.22M, a land-only-CV new build at $3.9M. The signature is precise:
    fair_value ≈ 0.95 × the AVM AND far above the CV.

    The current code NEVER produces fair_value = 0.95 × AVM when a CV is present
    (it drops the AVM from the anchor, and prices land-only CVs from comps), so a
    correctly re-priced batch — including comp-valued new builds that legitimately
    sit well above their land-only CV — will not match. That makes the heal
    converge after one pass instead of re-pricing every boot."""
    P = PropertyForSale
    avm = func.coalesce(P.pv_estimate_mid, P.homes_valuation)
    q = (db.query(P.id)
         .filter(P.import_batch_id == batch_id,
                 P.cv_numeric.isnot(None), P.cv_numeric > 0,
                 P.fair_value.isnot(None),
                 P.fair_value > 1.6 * P.cv_numeric,
                 avm.isnot(None), avm > 0,
                 func.abs(P.fair_value - 0.95 * avm) <= 0.02 * P.fair_value)
         .limit(1))
    return db.query(q.exists()).scalar()


def _has_correctable_cv(db: Session, batch_id: int) -> bool:
    """Any row whose stored CV disagrees with CoreLogic's already-fetched rating
    value (pv_cv) by more than the correction tolerance. Cheap existence check —
    no API calls; the pv_cv was captured by a prior enrich."""
    P = PropertyForSale
    q = (db.query(P.id)
         .filter(P.import_batch_id == batch_id,
                 P.pv_cv.isnot(None), P.pv_cv > 0,
                 P.cv_numeric.isnot(None), P.cv_numeric > 0,
                 func.abs(P.cv_numeric - P.pv_cv) > _CV_CORRECT_TOL * P.pv_cv)
         .limit(1))
    return db.query(q.exists()).scalar()


def _correct_cvs_from_pv(db: Session, batch_id: int) -> int:
    """Correct stored CVs from CoreLogic's already-fetched rating value (pv_cv),
    using data already on the row — no API calls. Returns count corrected."""
    rows = (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch_id,
                    PropertyForSale.pv_cv.isnot(None)).all())
    n = 0
    for p in rows:
        new_cv = corrected_cv(p.cv_numeric, p.pv_cv)
        if new_cv is not None:
            p.cv_numeric = new_cv
            n += 1
    if n:
        db.commit()
    return n


def auto_reprice_stale_batches() -> int:
    """Self-heal on startup: any staged for-sale batch still holding CV-over
    valuations from a pre-guard pricing run, OR a stored CV that CoreLogic's
    fetched rating value contradicts, is corrected + re-priced in place, once, so a
    deploy fixes the stored numbers without anyone having to click a button. Skips
    batches that are already clean or already being re-priced, and NEVER raises — a
    self-heal must not break boot. Returns batches repriced."""
    healed = 0
    db = SessionLocal()
    try:
        batches = (db.query(ImportBatch)
                   .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                           ImportBatch.status.in_(WORKING_STATUSES))
                   .order_by(ImportBatch.id.desc()).all())
        for b in batches:
            try:
                if stage_running(db, b.id, "price"):
                    continue
                stale_avm = _has_stale_avm_rows(db, b.id)
                bad_cv = _has_correctable_cv(db, b.id)
                if not stale_avm and not bad_cv:
                    continue
                region = b.region or "Auckland"
                fixed_cv = _correct_cvs_from_pv(db, b.id) if bad_cv else 0
                print(f"[auto_reprice] batch {b.id}: {fixed_cv} CV(s) corrected from "
                      f"CoreLogic, re-pricing in background ({region})", flush=True)
                res = reprice_batch(db, b.id, region=region, commit=True)
                if not res.error:
                    hold_flagged_rows(db, b.id)
                    healed += 1
                    print(f"[auto_reprice] batch {b.id} repriced: {res.rows} rows, "
                          f"{res.changed_fair_value} valuations changed", flush=True)
                else:
                    print(f"[auto_reprice] batch {b.id} skipped: {res.error}", flush=True)
            except Exception as e:
                print(f"[auto_reprice] batch {b.id} failed: {type(e).__name__}: {e}",
                      flush=True)
    except Exception as e:
        print(f"[auto_reprice] could not scan batches: {type(e).__name__}: {e}", flush=True)
    finally:
        db.close()
    return healed
