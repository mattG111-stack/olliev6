"""A question is answered when it is answered, not when the clock says so.

    "no the process needs to be a user askes a question then ollie does not
     time out till it answers it you could even count in a small corner 0-100%
     till your answer"

The old shape answered inside the request that asked. That put a proxy's
patience — not the question — in charge of how long an answer was allowed to
take, so a hard question was cut off mid-flight and reported to the browser as

    500 from /api/assistant: HTTP 500 with no response body

which is a dropped connection wearing a crash's clothes. The defence was a
55-second self-imposed deadline: give up first, and at least say so. That is an
honest answer to the wrong question.

So asking is a job now. POST /ask writes the question down and hands back an
id in milliseconds; a worker thread answers it with `deadline=None`; GET
/ask/{id} reports how far along it is. Nothing holds a socket open, so nothing
can cut one, and the answer takes exactly as long as it takes.

What is tested here is the contract that makes that safe:
  - the request returns immediately, whatever the question costs
  - a slow answer still lands, in full
  - the percentage is real: it only rises, and 100 means an answer exists
  - a failure is REPORTED, never silently dropped — that was the original sin
  - a straggler worker cannot erase a finished answer
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main, settings_store
from app.assistant import providers
from app.db import get_db
from app.models import AssistantLog, User, UserRole, UserStatus
from app.routers import assistant as A
from app.security import current_user, require_active


@pytest.fixture()
def me(db_session):
    u = User(email="asker@test.local", password_hash="x",
             role=UserRole.USER.value, status=UserStatus.APPROVED.value,
             llm_provider="anthropic")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def client(db_session, me, monkeypatch):
    """Signed in, with a key configured — so nothing is refused at the door."""
    monkeypatch.setattr(A.keys, "decrypt", lambda _v: "sk-test-key")
    monkeypatch.setattr(settings_store, "shared_key", lambda _db: (None, None))
    monkeypatch.setattr(A.settings_store, "shared_key", lambda _db: (None, None))
    monkeypatch.setattr(
        A.settings_store, "quota_for",
        lambda _db, _u: {"shared": False, "configured": True,
                         "remaining": None, "limit": None})
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: me,
        require_active: lambda: me,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}


@pytest.fixture()
def slow_answer(monkeypatch):
    """An answer that takes far longer than any request would be allowed to.

    Two seconds rather than two minutes because a test suite has to finish —
    but it is longer than the whole old DEADLINE budget scaled down, and the
    point being proved is structural: the request is not the thing waiting.
    """
    def _slow(user, question, history=None, **kw):
        assert kw.get("deadline") is None, (
            "the job worker must ask with NO deadline — a deadline here is the "
            "timeout coming back in through a different door")
        on_step = kw.get("on_step")
        for _ in range(3):
            if on_step:
                on_step("thinking", "")
                on_step("tool", "query_data")
            time.sleep(0.05)
        if on_step:
            on_step("writing", "")
        return providers.Result(text="Papakura, by a street.",
                                tools_used=["query_data"], iterations=3,
                                queries=["SELECT 1"])

    monkeypatch.setattr(A, "ask", _slow)
    return _slow


def _wait(client, ask_id, *, seconds=20.0):
    """Poll the way the browser does, until it stops running."""
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/api/assistant/ask/{ask_id}").json()
        if last["status"] != "running":
            return last
        time.sleep(0.05)
    raise AssertionError(f"never finished; last state {last}")


# ---- the percentage ---------------------------------------------------------
def test_progress_only_ever_rises():
    """A bar that goes backwards is worse than no bar — it reads as work being
    undone."""
    seen = [A.progress_for(r) for r in range(0, 30)]
    assert seen == sorted(seen)


def test_progress_never_reaches_100_while_it_is_still_working():
    """100% means the answer is on the screen. Nothing else may claim it."""
    assert all(A.progress_for(r) < 100 for r in range(0, 200))
    assert A.progress_for(10_000) == A.ASK_CEILING


def test_progress_starts_above_zero_the_moment_it_is_accepted():
    """0% for the first few seconds reads as "nothing happened"."""
    assert 0 < A.progress_for(0) < 10


def test_a_finished_ask_reads_100(db_session):
    row = AssistantLog(question="q", status="done", progress_pct=100)
    db_session.add(row)
    db_session.commit()
    assert A.shown_progress(row, datetime.now(timezone.utc)) == 100


def test_an_old_row_with_no_status_reads_as_finished(db_session):
    """There are thousands of rows from before this existed. None of them is
    still running."""
    row = AssistantLog(question="q")           # status left NULL
    db_session.add(row)
    db_session.commit()
    assert A.shown_progress(row, datetime.now(timezone.utc)) == 100


def test_the_number_creeps_while_a_step_is_slow_but_never_overtakes_it():
    """A number frozen for a minute reads as a hang; a number that runs ahead
    of the work is a lie. It eases toward the NEXT milestone and stops short."""
    now = datetime.now(timezone.utc)
    row = AssistantLog(question="q", status="running", iterations=2,
                       progress_pct=A.progress_for(2))
    row.phase_at = now
    at_once = A.shown_progress(row, now)
    row.phase_at = now - timedelta(seconds=40)
    later = A.shown_progress(row, now)
    assert at_once == A.progress_for(2)
    assert later > at_once
    assert later < A.progress_for(3)


def test_creep_is_bounded_however_long_a_step_takes():
    now = datetime.now(timezone.utc)
    row = AssistantLog(question="q", status="running", iterations=1,
                       progress_pct=A.progress_for(1))
    row.phase_at = now - timedelta(hours=3)
    assert A.shown_progress(row, now) < A.progress_for(2)


# ---- the request ------------------------------------------------------------
def test_asking_comes_back_immediately(client, slow_answer):
    """THE FIX. The request does no work, so it cannot run out of time."""
    started = time.monotonic()
    r = client.post("/api/assistant/ask", json={"question": "Which suburbs sell fastest?"})
    took = time.monotonic() - started
    assert r.status_code == 200, r.text
    assert r.json()["ask_id"] > 0
    assert r.json()["status"] == "running"
    assert took < 1.0, f"the request itself took {took:.1f}s — it is doing the work"


def test_a_slow_answer_still_arrives_in_full(client, slow_answer):
    """It takes as long as it takes, and nothing truncates it."""
    ask_id = client.post("/api/assistant/ask", json={"question": "q"}).json()["ask_id"]
    final = _wait(client, ask_id)
    assert final["status"] == "done"
    assert final["answer"] == "Papakura, by a street."
    assert final["progress_pct"] == 100
    assert final["queries"] == ["SELECT 1"]
    assert final["tools_used"] == ["query_data"]


def test_the_answer_is_recorded_for_the_memory(db_session, client, slow_answer):
    """Every question and answer is the corpus Ollie learns from — a question
    answered through the job must land there like any other."""
    ask_id = client.post("/api/assistant/ask", json={"question": "q"}).json()["ask_id"]
    _wait(client, ask_id)
    db_session.expire_all()
    row = db_session.get(AssistantLog, ask_id)
    assert row.ok is True
    assert row.answer == "Papakura, by a street."
    assert row.status == "done"
    assert row.finished_at is not None


def test_a_question_in_flight_is_not_fed_back_as_memory(db_session, me):
    """It has no answer yet. Offering it as context would put a blank where a
    prior answer should be."""
    db_session.add(AssistantLog(user_id=me.id, question="still going",
                                answer=None, ok=True, status="running"))
    db_session.commit()
    assert A._recent_memory(db_session, me.id) == []


# ---- when it goes wrong -----------------------------------------------------
def test_a_failure_is_reported_not_dropped(client, monkeypatch):
    """The original sin: a question that broke came back as a bare 500 and the
    reason went nowhere. Now the reason is on the row and in the poll."""
    def _boom(user, question, history=None, **kw):
        raise ValueError("the model wrote SQL the database rejected")
    monkeypatch.setattr(A, "ask", _boom)

    ask_id = client.post("/api/assistant/ask", json={"question": "q"}).json()["ask_id"]
    final = _wait(client, ask_id)
    assert final["status"] == "failed"
    assert "the model wrote SQL the database rejected" in final["error"]
    assert final["answer"] is None


def test_a_provider_problem_says_what_to_do(client, monkeypatch):
    def _rejected(user, question, history=None, **kw):
        raise providers.ProviderError("That API key was rejected. Check it in Settings.")
    monkeypatch.setattr(A, "ask", _rejected)

    ask_id = client.post("/api/assistant/ask", json={"question": "q"}).json()["ask_id"]
    final = _wait(client, ask_id)
    assert final["status"] == "failed"
    assert "Settings" in final["error"]


def test_a_failure_shows_in_the_admin_failure_list(db_session, client, monkeypatch):
    """Admin → Ask Ollie key is where the cause of a 500 is meant to be
    readable. A job failure has to reach it too."""
    def _boom(user, question, history=None, **kw):
        raise RuntimeError("tool exploded")
    monkeypatch.setattr(A, "ask", _boom)
    ask_id = client.post("/api/assistant/ask", json={"question": "q"}).json()["ask_id"]
    _wait(client, ask_id)
    db_session.expire_all()
    row = db_session.get(AssistantLog, ask_id)
    assert row.ok is False
    assert "tool exploded" in (row.answer or "")


def test_no_key_is_refused_at_the_door_not_after_a_poll(db_session, me, monkeypatch):
    """428 the moment they press it, so they are sent to Settings rather than
    watching a counter climb toward a failure."""
    monkeypatch.setattr(A.keys, "decrypt", lambda _v: None)
    monkeypatch.setattr(A.settings_store, "shared_key", lambda _db: (None, None))
    main.app.dependency_overrides = {
        get_db: lambda: db_session, current_user: lambda: me,
        require_active: lambda: me,
    }
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        assert c.post("/api/assistant/ask", json={"question": "q"}).status_code == 428
    finally:
        main.app.dependency_overrides = {}


def test_the_daily_allowance_is_checked_at_the_door(db_session, me, monkeypatch):
    monkeypatch.setattr(A.keys, "decrypt", lambda _v: "sk-test-key")
    monkeypatch.setattr(A.settings_store, "shared_key", lambda _db: ("anthropic", "sk-acct"))
    monkeypatch.setattr(
        A.settings_store, "quota_for",
        lambda _db, _u: {"shared": True, "configured": True,
                         "remaining": 0, "limit": 20})
    main.app.dependency_overrides = {
        get_db: lambda: db_session, current_user: lambda: me,
        require_active: lambda: me,
    }
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        r = c.post("/api/assistant/ask", json={"question": "q"})
        assert r.status_code == 429
        assert "20" in r.json()["detail"]
    finally:
        main.app.dependency_overrides = {}


# ---- who may read it --------------------------------------------------------
def test_nobody_else_can_read_your_question(db_session, client, slow_answer, me):
    """A question and its answer carry what someone is looking at and what they
    can afford. That is theirs."""
    ask_id = client.post("/api/assistant/ask", json={"question": "q"}).json()["ask_id"]
    _wait(client, ask_id)

    other = User(email="nosy@test.local", password_hash="x",
                 role=UserRole.USER.value, status=UserStatus.APPROVED.value)
    db_session.add(other)
    db_session.commit()
    main.app.dependency_overrides[current_user] = lambda: other
    main.app.dependency_overrides[require_active] = lambda: other
    assert client.get(f"/api/assistant/ask/{ask_id}").status_code == 404


def test_a_question_that_does_not_exist_is_a_404(client):
    assert client.get("/api/assistant/ask/999999").status_code == 404


# ---- the straggler ----------------------------------------------------------
def test_a_second_worker_cannot_erase_a_finished_answer(db_session, me, slow_answer):
    """The same trap the fill job has a guard for: a thread arriving late must
    stand down WITHOUT writing, or it stamps over a real answer."""
    now = datetime.now(timezone.utc)
    row = AssistantLog(user_id=me.id, question="q", status="running",
                       progress_pct=4, started_at=now, phase_at=now)
    db_session.add(row)
    db_session.commit()
    ask_id = row.id

    A._run_ask_job(ask_id, me.id)            # answers it
    A._run_ask_job(ask_id, me.id)            # a straggler arrives
    db_session.expire_all()
    done = db_session.get(AssistantLog, ask_id)
    assert done.status == "done"
    assert done.answer == "Papakura, by a street."


def test_a_late_progress_write_cannot_revive_a_finished_ask(db_session, me):
    """_mark only ever touches a row that is still running. Without that, a
    progress update landing after the answer puts the asker's screen back to
    counting toward something that has already arrived."""
    row = AssistantLog(user_id=me.id, question="q", status="done",
                       progress_pct=100, answer="done already")
    db_session.add(row)
    db_session.commit()

    A._mark(db_session, row.id, phase="Thinking it through", progress_pct=41,
            iterations=1)
    db_session.expire_all()
    after = db_session.get(AssistantLog, row.id)
    assert after.status == "done"
    assert after.progress_pct == 100
    assert after.answer == "done already"


# ---- the loop itself --------------------------------------------------------
def test_the_loop_has_no_clock_when_there_is_no_deadline():
    """_left returns infinity, so every "am I out of time" check below it is
    simply false — one rule rather than an `if untimed` beside each of four."""
    assert providers._left(0.0, None) == float("inf")
    assert providers._left(time.monotonic() - 10_000, None) == float("inf")


def test_a_deadline_still_bounds_the_synchronous_door():
    """The old endpoint still answers inside its request, so it must keep its
    budget — removing it there is how the bodyless 500 comes back."""
    assert providers._left(time.monotonic() - 999, providers.DEADLINE) < 0
    assert providers.DEADLINE < 60


def test_one_call_is_still_bounded_even_with_no_overall_deadline():
    """A socket that has silently died must not hang the worker for ever. The
    ceiling is generous rather than absent."""
    assert providers.CALL_TIMEOUT_UNBOUNDED > providers.DEADLINE
    assert providers.CALL_TIMEOUT_UNBOUNDED >= 120


def test_a_job_gets_more_room_to_work_than_a_request():
    assert providers.JOB_MAX_ITERATIONS > providers.MAX_ITERATIONS


def test_progress_reporting_cannot_break_an_answer():
    """_note swallows everything: a callback that writes to a database can fail
    for reasons that have nothing to do with the question."""
    def _explodes(kind, detail):
        raise RuntimeError("the database went away")
    providers._note(_explodes, "thinking", "")        # must not raise
    providers._note(None, "thinking", "")


def test_the_synchronous_endpoint_still_works(client, monkeypatch):
    """It is still there for anything that has not moved over, and it must not
    have been broken by the plumbing added around it."""
    monkeypatch.setattr(
        A, "ask",
        lambda *a, **k: providers.Result(text="Straight answer.", iterations=1))
    r = client.post("/api/assistant", json={"question": "q"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "Straight answer."


# ---- the ways it could still hang or overcharge -----------------------------
# Found by going looking rather than by it happening to somebody. "It never
# times out" is only true if there is no path where the asker waits for ever.
def test_a_question_orphaned_by_a_restart_does_not_poll_for_ever(db_session, client, me):
    """THE HOLE IN "IT NEVER TIMES OUT".

    A worker thread dies with its container — a redeploy, an OOM. Nothing is
    left to finish the row, so it stays "running" for ever, and the browser
    polls it for ever with the counter creeping toward a milestone that will
    never arrive. That is not "taking its time", it is a hang wearing patience
    as a disguise, and it is worse than the timeout it replaced because there
    is no end to it at all.
    """
    stale = datetime.now(timezone.utc) - timedelta(minutes=90)
    row = AssistantLog(user_id=me.id, question="q", status="running",
                       progress_pct=41, iterations=1,
                       started_at=stale, phase_at=stale)
    db_session.add(row)
    db_session.commit()

    got = client.get(f"/api/assistant/ask/{row.id}").json()
    assert got["status"] == "failed", "an abandoned question still reads as running"
    assert got["error"], "it must say what happened, not just stop"


def test_a_slow_step_is_not_mistaken_for_an_abandoned_one(db_session, client, me):
    """The whole point is that a question may take a long time. The threshold
    has to sit well beyond the longest a single step can legitimately take."""
    working = datetime.now(timezone.utc) - timedelta(minutes=4)
    row = AssistantLog(user_id=me.id, question="q", status="running",
                       progress_pct=41, iterations=1,
                       started_at=working, phase_at=working)
    db_session.add(row)
    db_session.commit()
    assert client.get(f"/api/assistant/ask/{row.id}").json()["status"] == "running"


def test_the_stale_threshold_clears_the_longest_legitimate_step():
    """One model call may take CALL_TIMEOUT_UNBOUNDED, and tool dispatch runs
    after it. A threshold under that would kill questions that are working."""
    from app.assistant import providers as P
    assert A.ASK_STALE_MINUTES * 60 > P.CALL_TIMEOUT_UNBOUNDED * 2


def test_an_abandoned_question_is_released_once_not_re_stamped(db_session, client, me):
    stale = datetime.now(timezone.utc) - timedelta(minutes=90)
    row = AssistantLog(user_id=me.id, question="q", status="running",
                       started_at=stale, phase_at=stale)
    db_session.add(row)
    db_session.commit()
    first = client.get(f"/api/assistant/ask/{row.id}").json()
    second = client.get(f"/api/assistant/ask/{row.id}").json()
    assert first["status"] == second["status"] == "failed"
    assert first["error"] == second["error"]


def test_an_unanswered_question_does_not_burn_the_daily_allowance(db_session, me):
    """A row is written the moment a question is ASKED now, not when it is
    answered — so counting rows counts questions that gave nobody anything.

    "burning someone's daily allowance on our own provider outage is the kind
     of small unfairness nobody can see the cause of" — used_today's own words.
    """
    from app import settings_store as S
    db_session.add(AssistantLog(user_id=me.id, question="in flight",
                                answer=None, ok=True, status="running"))
    db_session.add(AssistantLog(user_id=me.id, question="abandoned",
                                answer=None, ok=True, status="running"))
    db_session.commit()
    assert S.used_today(db_session, me.id) == 0


def test_an_answered_question_does_count(db_session, me):
    from app import settings_store as S
    db_session.add(AssistantLog(user_id=me.id, question="answered",
                                answer="Papakura.", ok=True, status="done"))
    db_session.commit()
    assert S.used_today(db_session, me.id) == 1


def test_an_empty_answer_is_not_served_as_an_answer(db_session, client, me, monkeypatch):
    """A model can come back with no text at all. Stored, that is a NULL answer
    on a "done" row, and the page renders an empty bubble — which reads as a
    broken product rather than as a question that did not land."""
    monkeypatch.setattr(
        A, "ask",
        lambda *a, **k: providers.Result(text="   ", tools_used=[], iterations=1))
    ask_id = client.post("/api/assistant/ask", json={"question": "q"}).json()["ask_id"]
    final = _wait(client, ask_id)
    assert final["status"] == "failed", "an empty answer was served as an answer"
    assert final["error"]
    assert final["answer"] is None


def test_a_huge_history_cannot_bloat_the_row(db_session, client, me, slow_answer):
    """History is context, not data. The question is capped at 2000 characters
    and the history was not capped at all, so twenty turns of arbitrary length
    went into the row verbatim."""
    huge = [{"role": "user", "content": "x" * 200_000},
            {"role": "assistant", "content": "y" * 200_000}]
    r = client.post("/api/assistant/ask", json={"question": "q", "history": huge})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    row = db_session.get(AssistantLog, r.json()["ask_id"])
    assert len(row.queries or "") < 100_000, "the whole history went in verbatim"
