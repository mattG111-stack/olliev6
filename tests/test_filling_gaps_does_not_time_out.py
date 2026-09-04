"""Filling the gaps on new listings, a chunk at a time.

    "[browser] 500 from /api/admin/release/listings/fill: HTTP 500 with no
     response body — the request was cut off before the server answered (it may
     have taken too long)."   — the bug log, four times over

It was never a 500. `fill_pending` looked up as many as 400 pending listings in
one request, each one a real call to the council-record service taking a second
or more, and the gateway closed the connection minutes before the work
finished. What the browser reported as a crash was a timeout.

    "it should never time out it should just take its time"

So it does not run in the request at all. The endpoint starts a JOB and answers
in milliseconds, exactly as the portal sweep beside it already did, and the
work runs on its own thread until it is finished — half an hour if there is
half an hour of it. Closing the browser does not stop it.

Chunking survives inside that worker, because a chunk is what makes progress
reportable and bounds each transaction.

The trap in a design like this is the stop condition. "Rows that still need
filling" cannot be it: a row whose lookup FAILED still needs filling, so it
would be picked again by the next call, and the caller would loop on it for
ever — a hang that looks exactly like the timeout being fixed. The cursor is
the high-water mark of what has been SCANNED, needed or not, so it always moves
forward.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import PortalListing
from app.portals import complete


@pytest.fixture()
def pending(db_session):
    """Sixty pending listings, none of which carry a council record."""
    rows = [
        PortalListing(
            source="oneroof", kind="for_sale", status="pending",
            url=f"https://example.test/{i}",
            address=f"{i} Example Road", suburb="Papakura",
            price_numeric=900_000.0 + i,
        )
        for i in range(60)
    ]
    db_session.add_all(rows)
    db_session.commit()
    return rows


@pytest.fixture()
def never_reachable(monkeypatch):
    """The lookup service is down — the worst case for a cursor, because every
    row still needs filling after it has been tried."""
    calls = {"n": 0}

    def _boom(db, row):
        calls["n"] += 1
        return 0, "unreachable"

    monkeypatch.setattr(complete, "fill_one", _boom)
    return calls


def test_one_call_does_a_bounded_amount_of_work(db_session, pending, never_reachable):
    """THE BUG. 400 lookups in one request is minutes behind a gateway that
    waits seconds."""
    out = complete.fill_pending(db_session, kind="for_sale")
    assert out["scanned"] <= complete.CHUNK
    assert never_reachable["n"] <= complete.CHUNK


def test_the_caller_can_ask_for_more_but_not_unboundedly(db_session, pending,
                                                         never_reachable):
    out = complete.fill_pending(db_session, kind="for_sale", limit=10_000)
    assert out["scanned"] <= complete.MAX_CHUNK


def test_walking_the_cursor_covers_every_row_exactly_once(db_session, pending,
                                                          never_reachable):
    after, seen, rounds = 0, 0, 0
    while rounds < 100:
        rounds += 1
        out = complete.fill_pending(db_session, kind="for_sale", after_id=after)
        if not out["scanned"]:
            break
        assert out["last_id"] > after, "the cursor did not move — this is the hang"
        after = out["last_id"]
        seen += out["scanned"]
    assert seen == 60
    assert never_reachable["n"] == 60, "a row was looked up twice, or skipped"


def test_it_terminates_even_when_every_lookup_fails(db_session, pending,
                                                    never_reachable):
    """The one that matters. Every row still needs filling after being tried,
    so a "rows that need filling" cursor would never finish."""
    after, rounds = 0, 0
    while rounds < 500:
        rounds += 1
        out = complete.fill_pending(db_session, kind="for_sale", after_id=after)
        if not out["scanned"]:
            break
        after = out["last_id"]
    assert rounds < 500, "it never finished — the caller would loop for ever"
    assert rounds <= 5, f"took {rounds} rounds for 60 rows at {complete.CHUNK} a time"


def test_remaining_counts_down(db_session, pending, never_reachable):
    """It drives the progress line. A number that does not fall is worse than
    no number, because it reads as stuck."""
    first = complete.fill_pending(db_session, kind="for_sale")
    second = complete.fill_pending(db_session, kind="for_sale",
                                   after_id=first["last_id"])
    assert first["remaining"] == 60 - first["scanned"]
    assert second["remaining"] < first["remaining"]


def test_the_last_chunk_reports_nothing_left(db_session, pending, never_reachable):
    after = 0
    for _ in range(10):
        out = complete.fill_pending(db_session, kind="for_sale", after_id=after)
        if not out["scanned"]:
            break
        after = out["last_id"]
    assert out["scanned"] == 0
    assert out["remaining"] == 0


def test_an_empty_queue_answers_immediately(db_session):
    out = complete.fill_pending(db_session, kind="for_sale")
    assert out == {
        "looked_up": 0, "fields_filled": 0, "council_records_found": 0,
        "not_found": 0, "blocked": 0, "unreachable": 0,
        "last_id": 0, "remaining": 0, "scanned": 0,
    }


def test_rows_already_complete_are_scanned_but_not_looked_up(db_session,
                                                             never_reachable):
    """A lookup we do not need is a second of somebody's afternoon and a call
    against the allowance — but the row still has to advance the cursor, or the
    walk stalls on it."""
    done = PortalListing(
        source="oneroof", kind="for_sale", status="pending",
        url="https://example.test/full", address="1 Complete Way",
        suburb="Papakura", price_numeric=1_000_000.0,
        # Every field in complete._FILLABLE, so needs_filling() says no.
        cv_numeric=1_100_000.0, land_value_numeric=700_000.0,
        improvement_value_numeric=400_000.0, floor_area_m2=150.0,
        land_area_m2=600.0, beds=3, baths=2, carspaces=1,
        prior_sale_price=880_000.0, prior_sale_date="2019-03-04",
        property_type="House", image_url="https://example.test/p.jpg",
    )
    db_session.add(done)
    db_session.commit()
    out = complete.fill_pending(db_session, kind="for_sale")
    assert out["scanned"] == 1
    assert out["looked_up"] == 0
    assert never_reachable["n"] == 0
    assert out["last_id"] == done.id


def test_a_different_kind_is_left_alone(db_session, pending, never_reachable):
    sold = PortalListing(source="oneroof", kind="sold", status="pending",
                         url="https://example.test/sold",
                         address="9 Sold Street", suburb="Papakura")
    db_session.add(sold)
    db_session.commit()
    after, scanned = 0, 0
    for _ in range(10):
        out = complete.fill_pending(db_session, kind="for_sale", after_id=after)
        if not out["scanned"]:
            break
        after, scanned = out["last_id"], scanned + out["scanned"]
    assert scanned == 60


# ---- through the endpoint ---------------------------------------------------
def _client(db_session, user):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.security import current_user, require_active, require_admin

    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: user,
        require_active: lambda: user,
        require_admin: lambda: user,
    }
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture()
def admin(db_session):
    from app import main
    from app.models import User, UserRole, UserStatus

    u = User(email="ops@test.local", password_hash="x",
             role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    try:
        yield _client(db_session, u)
    finally:
        main.app.dependency_overrides = {}


def test_the_endpoint_answers_immediately_with_a_job(admin, pending, never_reachable):
    """THE FIX. The request does not do the work, so it cannot run out of time.

    "it should never time out it should just take its time" — so the answer is
    a job id in milliseconds, and the looking-up happens behind it for as long
    as it needs.
    """
    r = admin.post("/api/admin/release/listings/fill?kind=for_sale")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] > 0
    assert body["stage"] == "filling"


def test_the_job_finishes_every_row(db_session, admin, pending, never_reachable):
    """Run the worker on this thread so the test can watch it finish. Sixty
    rows, none reachable — the worst case — and it still completes."""
    from app.models import IngestJob
    from app.routers.release import _run_fill_job

    from app.staged_stages import create_stage_job

    # The job is made directly rather than through the endpoint: the endpoint
    # starts a real thread on the same id, and two workers racing one job is a
    # test that fails for a reason unrelated to what it is checking.
    job = create_stage_job(db_session, stage="filling", batch_id=None,
                           region="Auckland", uploaded_by_id=None)
    jid = job.id
    _run_fill_job(jid, kind="for_sale")
    db_session.expire_all()
    job = db_session.query(IngestJob).get(jid)
    assert job.status == "completed", job.error_message
    assert job.progress_pct == 100
    assert never_reachable["n"] == 60, "every row should have been tried once"


def test_a_second_press_will_not_start_a_second_run(db_session, admin, pending,
                                                    never_reachable):
    """Two threads on the same rows is the same lookup billed twice, and the
    way this container has been OOM-killed before.

    Written against the RUNNING JOB rather than against the clock. Pressing
    twice in a row and expecting the second press to be refused assumes the
    first worker is still going when the second request lands — it is a daemon
    thread, and on a loaded machine it can finish first, which failed this once
    in a full-suite run while passing every time the file was run on its own.
    A test that depends on losing a race proves nothing on the runs it wins.

    So the first run is left genuinely in flight: its job row says running, the
    way stage_running reads it, and the second press has to be refused.
    """
    from app.models import IngestJob

    first = admin.post("/api/admin/release/listings/fill?kind=for_sale")
    assert first.status_code == 200
    job = db_session.get(IngestJob, first.json()["job_id"])
    job.status = "running"
    job.last_progress_at = datetime.now(timezone.utc)   # beating, not abandoned
    db_session.commit()

    second = admin.post("/api/admin/release/listings/fill?kind=for_sale")
    assert second.status_code == 409, second.text


def test_a_run_whose_container_died_does_not_lock_the_button(db_session, admin,
                                                             pending, never_reachable):
    """A job left "running" by a restart has to be released, or the button is
    dead until somebody edits the database."""
    from datetime import timedelta

    from app.models import IngestJob

    started = admin.post("/api/admin/release/listings/fill?kind=for_sale").json()
    job = db_session.query(IngestJob).get(started["job_id"])
    job.status = "running"
    stale = datetime.now(timezone.utc) - timedelta(hours=3)
    job.started_at = stale
    job.last_progress_at = stale
    db_session.commit()

    again = admin.post("/api/admin/release/listings/fill?kind=for_sale")
    assert again.status_code == 200, again.text


def test_a_customer_cannot_spend_the_lookup_allowance(db_session, pending):
    """Every call costs money against the council-record account."""
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import current_user, require_active

    u = User(email="punter@test.local", password_hash="x",
             role=UserRole.USER.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: u,
        require_active: lambda: u,
    }
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        assert c.post("/api/admin/release/listings/fill").status_code in (401, 403)
    finally:
        main.app.dependency_overrides = {}


def test_a_second_worker_never_erases_a_finished_run(db_session, pending,
                                                     never_reachable):
    """job_was_cancelled is true for ANY status that is not "running", and that
    includes "completed". A second thread noticing a finished job must stand
    down silently rather than stamping "cancelled" over the result."""
    from app.models import IngestJob
    from app.routers.release import _run_fill_job
    from app.staged_stages import create_stage_job

    job = create_stage_job(db_session, stage="filling", batch_id=None,
                           region="Auckland", uploaded_by_id=None)
    jid = job.id
    _run_fill_job(jid, kind="for_sale")          # finishes it
    _run_fill_job(jid, kind="for_sale")          # a straggler arrives
    db_session.expire_all()
    assert db_session.query(IngestJob).get(jid).status == "completed"
