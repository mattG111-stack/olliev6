"""A stage that stops has to be able to start again.

    "call logic timed out and wouldn't restart ... it just timed out and then
     had a red error message"

Three separate faults, and the first one caused the other two.

  THE STOPWATCH. "Is this run still alive" was answered by looking at
  started_at, which is stamped once when the run begins and never moves again.
  That does not measure whether a job is alive; it measures how long ago it
  started. Twenty minutes in, EVERY enrich was declared abandoned — including
  one that was working perfectly, which a full batch always is, because a full
  batch takes hours. That is the red error on a screen whose run was still
  going.

  THE STACKING. Being declared abandoned also released the lock. So pressing
  Enrich again started a second enrich on top of the first one, which was still
  running: two heavy workers in a container sized for one. That is how it gets
  OOM-killed, and an OOM kill is how "it timed out" turns into "and now it will
  not restart" — because the next run inherits a job row stuck at "running"
  with nobody behind it.

  THE DEAD END. While a run was inside its twenty minutes, the button answered
  "Enrich is already running for this batch" and kept answering it. There was
  no way to say "I know, it is stuck, take it off and start again" — the only
  cure was to wait the lock out.

What replaces them: a heartbeat, so a working run is never called dead however
long it takes and a stopped one is spotted in minutes; a restart that takes the
stage off whoever is holding it; and, because the two ways enrich stops early
are both temporary, a run that waits and picks itself back up without anyone
pressing anything.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (BatchType, ImportBatch, IngestJob, PropertyForSale)
from app.propertyvalue import PV_ERROR, PV_OK


def _batch(db, rows=3, status="staged"):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="auckland.csv", is_active=False, status=status)
    db.add(b)
    db.flush()
    for i in range(rows):
        db.add(PropertyForSale(
            import_batch_id=b.id, address=f"{i} Restart Road", suburb="Riverhead",
            asking_price=900_000.0, cv_numeric=1_000_000.0,
            floor_area_m2=None, land_area_m2=None))     # blank -> needs a lookup
    db.commit()
    return b


def _job(db, batch, *, stage="enrich", status="running",
         started_ago_min=0.0, beat_ago_min=None):
    now = datetime.now(timezone.utc)
    j = IngestJob(batch_type=BatchType.FOR_SALE.value, filename="auckland.csv",
                  file_size_bytes=1, status=status, stage=stage, batch_id=batch.id,
                  started_at=now - timedelta(minutes=started_ago_min),
                  last_progress_at=(None if beat_ago_min is None
                                    else now - timedelta(minutes=beat_ago_min)))
    db.add(j)
    db.commit()
    return j


# ---- the stopwatch ----------------------------------------------------------
def test_a_run_that_is_still_working_is_never_declared_dead(db_session):
    """THE BUG. A full enrich runs for hours. Ninety minutes in, still beating,
    it was called abandoned — which is where the red error came from, on a run
    that was doing exactly what it was supposed to."""
    from app.staged_stages import stage_running

    b = _batch(db_session)
    job = _job(db_session, b, started_ago_min=90, beat_ago_min=1)

    assert stage_running(db_session, b.id, "enrich") is True, \
        "a run that beat one minute ago was declared dead because it started long ago"
    db_session.refresh(job)
    assert job.status == "running", "a live run had its job row failed underneath it"


def test_a_run_that_has_stopped_beating_is_released(db_session):
    """The counterweight, and the reason the check exists at all: a container
    killed mid-run leaves a row saying "running" with nobody behind it, and that
    must not hold the button for ever."""
    from app.staged_stages import stage_running

    b = _batch(db_session)
    job = _job(db_session, b, started_ago_min=90, beat_ago_min=45)

    assert stage_running(db_session, b.id, "enrich") is False
    db_session.refresh(job)
    assert job.status == "failed"
    assert "abandoned" in (job.error_message or "")


def test_a_job_from_before_heartbeats_falls_back_to_its_start_time(db_session):
    """Rows written by the old build have no heartbeat to read. They must not
    all read as dead — that would release a genuinely running enrich the moment
    this deploys."""
    from app.staged_stages import stage_running

    b = _batch(db_session)
    _job(db_session, b, started_ago_min=5, beat_ago_min=None)
    assert stage_running(db_session, b.id, "enrich") is True

    b2 = _batch(db_session)
    _job(db_session, b2, started_ago_min=45, beat_ago_min=None)
    assert stage_running(db_session, b2.id, "enrich") is False


def test_every_progress_write_counts_as_a_heartbeat(db_session):
    """The heartbeat is stamped in one place — the shared update — so that no
    future progress write can forget to say "still alive". A missed beat now
    reads as a dead job, so forgetting one is not a small mistake."""
    from app.staged_stages import _update

    b = _batch(db_session)
    job = _job(db_session, b, started_ago_min=30, beat_ago_min=30)

    _update(db_session, job.id, progress_pct=42)

    db_session.refresh(job)
    assert job.progress_pct == 42
    age = datetime.now(timezone.utc) - job.last_progress_at.replace(tzinfo=timezone.utc)
    assert age < timedelta(minutes=1), "a progress write did not count as a heartbeat"


# ---- the restart ------------------------------------------------------------
def test_restarting_takes_the_stage_off_the_run_holding_it(db_session):
    from app.staged_stages import abandon_stage, stage_running

    b = _batch(db_session)
    job = _job(db_session, b, started_ago_min=2, beat_ago_min=0.1)
    assert stage_running(db_session, b.id, "enrich") is True, "fixture is not holding the lock"

    assert abandon_stage(db_session, b.id, "enrich") == 1
    db_session.refresh(job)
    assert job.status == "failed"
    assert "restart" in (job.error_message or "")
    assert stage_running(db_session, b.id, "enrich") is False, "the lock survived the restart"


def test_restarting_when_nothing_is_running_is_just_a_start(db_session):
    """Pressing it on a quiet batch must not be an error — an operator cannot
    always tell whether the thing is stuck or finished, and being punished for
    guessing wrong teaches them not to touch it."""
    from app.staged_stages import abandon_stage

    b = _batch(db_session)
    assert abandon_stage(db_session, b.id, "enrich") == 0


def test_the_stopped_worker_is_told_to_stand_down(db_session):
    """A thread cannot be killed from outside, so it has to agree to stop.
    Without this, a forced restart leaves the old enrich running underneath the
    new one — two workers on one batch, which is the thing the lock exists to
    prevent."""
    from app.staged_stages import abandon_stage, job_was_cancelled

    b = _batch(db_session)
    job = _job(db_session, b, started_ago_min=2, beat_ago_min=0.1)
    assert job_was_cancelled(db_session, job.id) is False

    abandon_stage(db_session, b.id, "enrich")
    assert job_was_cancelled(db_session, job.id) is True


def test_a_waiting_worker_wakes_up_to_notice_a_restart(db_session, monkeypatch):
    """The rate-limit ladder waits up to five minutes and the retry ladder up to
    fifteen. A restart pressed during one of those must not be ignored until the
    wait ends."""
    import app.staged_stages as st

    b = _batch(db_session)
    job = _job(db_session, b, started_ago_min=1, beat_ago_min=0.1)

    slept = []

    def _fake_sleep(sec):
        slept.append(sec)
        if len(slept) == 1:                      # someone presses Restart mid-wait
            st.abandon_stage(db_session, b.id, "enrich")

    monkeypatch.setattr(st.time, "sleep", _fake_sleep)

    assert st._sleep_alive(db_session, job.id, 900) is False
    assert len(slept) == 1, "it slept out the whole wait instead of checking"


def test_a_long_wait_keeps_beating_while_it_waits(db_session, monkeypatch):
    """A quarter of an hour of silence looks exactly like a hang, and the
    liveness check would agree with that reading and release the lock."""
    import app.staged_stages as st

    b = _batch(db_session)
    job = _job(db_session, b, started_ago_min=1, beat_ago_min=20)
    monkeypatch.setattr(st.time, "sleep", lambda *_: None)

    assert st._sleep_alive(db_session, job.id, 120) is True

    db_session.refresh(job)
    age = datetime.now(timezone.utc) - job.last_progress_at.replace(tzinfo=timezone.utc)
    assert age < timedelta(minutes=1), "it went quiet for the whole wait"


# ---- retrying by itself -----------------------------------------------------
def _run(db, batch, job, monkeypatch, answers, *, retry_waits=(0, 0, 0)):
    """Run the stage with CoreLogic replaced by a canned sequence, and the waits
    set to nothing so a test does not sit through the real ladder."""
    import app.staged_stages as st

    seq = list(answers)
    monkeypatch.setattr(st, "pv_lookup_status",
                        lambda q, *a, **k: seq.pop(0) if seq else (None, PV_ERROR))
    monkeypatch.setattr(st.time, "sleep", lambda *_: None)
    db.commit()
    out = st.run_enrich_job(job.id, batch.id, "Auckland", delay=0,
                            retry_waits=retry_waits)
    db.expire_all()
    return out, db.get(IngestJob, job.id)


def test_an_outage_that_clears_is_picked_up_without_anyone_pressing_anything(
        db_session, monkeypatch):
    """THE POINT OF ALL THIS. The network drops, the run stops, and until now it
    sat there red until a person noticed. Overnight that is the whole night."""
    b = _batch(db_session, rows=45)
    good = ({"floor_area_m2": 150.0, "land_area_m2": 600.0}, PV_OK)
    # First attempt: total outage, enough of it to trip the circuit breaker.
    # Second: the network is back.
    out, job = _run(db_session, b, _job(db_session, b, status="pending"),
                    monkeypatch, [(None, PV_ERROR)] * 40 + [good] * 200)

    assert out == "done", f"it gave up instead of retrying (ended {out!r})"
    assert job.status == "completed"
    rows = db_session.query(PropertyForSale).filter_by(import_batch_id=b.id).all()
    assert any(p.floor_area_m2 == 150.0 for p in rows), \
        "the retry did not actually fill anything"


def test_it_gives_up_eventually_rather_than_retrying_for_ever(db_session, monkeypatch):
    """Nothing still broken after the ladder is going to be fixed by another
    lap, and a job that retries for ever never reports the outage it is stuck
    on."""
    b = _batch(db_session, rows=45)
    out, job = _run(db_session, b, _job(db_session, b, status="pending"),
                    monkeypatch, [(None, PV_ERROR)] * 400)

    assert out == "stop"
    assert job.status == "failed"
    assert "before reaching CoreLogic" in (job.error_message or "")


def test_a_retry_resumes_and_does_not_reask_what_it_already_answered(
        db_session, monkeypatch):
    """Retrying is only safe because the pass resumes. A retry loop over a job
    that started from the top would spend the same lookups again every lap and
    never reach the end of a real batch."""
    import app.staged_stages as st

    b = _batch(db_session, rows=45)
    good = ({"floor_area_m2": 150.0, "land_area_m2": 600.0}, PV_OK)

    asked: list[str] = []
    seq = [good] * 5 + [(None, PV_ERROR)] * 40 + [good] * 200

    def _spy(q, *a, **k):
        asked.append(q)
        return seq.pop(0) if seq else (None, PV_ERROR)

    monkeypatch.setattr(st, "pv_lookup_status", _spy)
    monkeypatch.setattr(st.time, "sleep", lambda *_: None)
    job = _job(db_session, b, status="pending")
    db_session.commit()
    st.run_enrich_job(job.id, b.id, "Auckland", delay=0, retry_waits=(0, 0, 0))

    first_five = asked[:5]
    after_retry = asked[5:]
    assert first_five, "nothing was asked at all"
    assert not (set(first_five) & set(after_retry)), \
        "the retry paid for addresses the first attempt had already answered"


def test_a_clean_run_does_not_retry(db_session, monkeypatch):
    """A retry ladder that fires on a healthy run turns one enrich into four."""
    b = _batch(db_session, rows=3)
    good = ({"floor_area_m2": 150.0, "land_area_m2": 600.0}, PV_OK)
    out, job = _run(db_session, b, _job(db_session, b, status="pending"),
                    monkeypatch, [good] * 3)

    assert out == "done"
    assert job.status == "completed"
    assert job.rows_inserted == 3, "it looked up more than the batch holds"


def test_a_restart_during_a_run_stops_it_rather_than_stacking_on_it(
        db_session, monkeypatch):
    """The OOM. Two enrichers on one batch is what killed the container, and a
    restart is exactly the moment that would happen."""
    import app.staged_stages as st

    b = _batch(db_session, rows=45)
    job = _job(db_session, b, status="pending")

    calls = {"n": 0}

    def _spy(q, *a, **k):
        calls["n"] += 1
        if calls["n"] == 6:                       # someone presses Restart mid-run
            st.abandon_stage(db_session, b.id, "enrich")
        return ({"floor_area_m2": 150.0}, PV_OK)

    monkeypatch.setattr(st, "pv_lookup_status", _spy)
    monkeypatch.setattr(st.time, "sleep", lambda *_: None)
    db_session.commit()

    out = st.run_enrich_job(job.id, b.id, "Auckland", delay=0, retry_waits=(0,))

    assert out == "cancelled", f"the old worker carried on after a restart ({out!r})"
    assert calls["n"] < 45, "it worked through the whole batch after being stopped"


# ---- the other long stage ---------------------------------------------------
def test_repricing_beats_while_it_works(db_session):
    """Re-pricing a full batch is one long call between two progress writes. The
    liveness check reads silence as death, so fixing this for enrich and leaving
    the price stage silent would just move the fault.

    Asserted where the beat is produced — reprice_batch calling back per chunk —
    because that is the part that can be removed without anything else noticing.
    """
    import inspect

    from app import reprice, staged_stages

    assert "on_chunk" in inspect.signature(reprice.reprice_batch).parameters, \
        "reprice cannot report progress, so a long run looks like a hung one"

    src = inspect.getsource(reprice.reprice_batch)
    assert "on_chunk(res.rows, len(ids))" in src, "the callback is never called"

    price_src = inspect.getsource(staged_stages.run_price_job)
    assert "on_chunk=" in price_src, "the price stage does not pass a heartbeat in"


def test_a_beat_that_throws_does_not_lose_the_batch(db_session):
    """The heartbeat is bookkeeping. A re-priced batch is the work. If the
    bookkeeping can take the work down with it, the cure is worse."""
    from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold
    from app.reprice import reprice_batch

    sold = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                       filename="sold.xlsx", status="published", is_active=True)
    db_session.add(sold)
    db_session.flush()
    for i in range(40):
        db_session.add(PropertySold(
            import_batch_id=sold.id, address=f"{i} Comparable Way", suburb="Riverhead",
            sale_price=1_000_000.0, floor_area_m2=150.0, land_area_m2=600.0,
            beds=3, baths=2, property_type="House"))
    b = _batch(db_session, rows=0)
    for i in range(5):
        db_session.add(PropertyForSale(
            import_batch_id=b.id, address=f"{i} Priced Place", suburb="Riverhead",
            asking_price=900_000.0, cv_numeric=1_000_000.0,
            floor_area_m2=150.0, land_area_m2=600.0, beds=3, baths=2,
            property_type="House"))
    db_session.commit()

    def _explode(done, total):
        raise RuntimeError("the heartbeat blew up")

    res = reprice_batch(db_session, b.id, region="Auckland", commit=True,
                        on_chunk=_explode)
    assert res.error is None, res.error
    assert res.rows == 5, "a failing heartbeat threw away the re-priced batch"


# ---- through the actual button ----------------------------------------------
def test_the_restart_endpoint_starts_a_new_run_over_a_stuck_one(db_session, admin_client,
                                                                monkeypatch):
    """The dead end, through HTTP: a batch whose enrich is stuck inside its
    window. The ordinary button refuses; Restart gets through."""
    import app.routers.release as rel
    monkeypatch.setattr(rel.threading, "Thread", _NoThread)

    b = _batch(db_session, rows=2)
    stuck = _job(db_session, b, started_ago_min=2, beat_ago_min=0.1)

    refused = admin_client.post("/api/admin/release/enrich")
    assert refused.status_code == 409, "the fixture is not actually stuck"

    r = admin_client.post("/api/admin/release/enrich/restart")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stopped"] == 1, body
    assert body["job_id"] != stuck.id, "it reused the stuck job instead of starting one"

    db_session.expire_all()
    assert db_session.get(IngestJob, stuck.id).status == "failed"


def test_the_restart_endpoint_refuses_a_stage_it_does_not_have(admin_client):
    r = admin_client.post("/api/admin/release/publish/restart")
    assert r.status_code == 404


class _NoThread:
    """Swallow the background thread so the test asserts on the endpoint rather
    than racing a real enrich against SQLite."""

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


@pytest.fixture()
def admin_client(db_session):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import current_user, require_active, require_admin

    admin = User(email="restart-admin@test.local", password_hash="x",
                 role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(admin)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: admin,
        require_active: lambda: admin,
        require_admin: lambda: admin,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}
