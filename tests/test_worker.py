"""Scheduled work runs in its own process, and cannot take the API down.

The API's job is to answer requests. The scheduled work is the opposite shape —
long, memory-hungry, and reaching out to other people's servers — and sharing a
container makes every one of those a way for the site to go down. It already
has: the note in staged_stages.run_enrich_job records a few stacked runs
OOM-killing the container, and a container that dies takes the API with it
whether or not the API caused it.

One codebase though, deliberately. The models, the pricing and the hold rules
are shared; two apps would be two definitions of what a property is, drifting
apart one deploy at a time.

What is tested here is the property that makes the split worth having: this loop
survives its jobs. A job that raises, hangs on a bad return, or is switched off
must not stop the next one, or tomorrow's.
"""
from __future__ import annotations

from app.worker import Job, run


def _collect():
    seen: list[str] = []
    return seen, lambda: seen.append("ran")


def test_a_due_job_runs():
    seen, fn = _collect()
    run([Job("t", every_seconds=0, fn=fn)], max_ticks=1, sleep=lambda _s: None)
    assert seen == ["ran"]


def test_a_job_that_is_not_due_yet_does_not_run_again():
    seen, fn = _collect()
    job = Job("t", every_seconds=10_000, fn=fn)
    run([job], max_ticks=3, sleep=lambda _s: None)
    assert seen == ["ran"], "a daily job ran three times in three minutes"


def test_a_switched_off_job_never_runs():
    seen, fn = _collect()
    run([Job("t", every_seconds=0, fn=fn, enabled=lambda: False)],
        max_ticks=2, sleep=lambda _s: None)
    assert seen == []


def test_a_job_that_raises_does_not_stop_the_others():
    """The whole reason this is a separate process is that things fail."""
    seen, fn = _collect()

    def broken():
        raise RuntimeError("the portal hung up")

    run([Job("broken", 0, broken), Job("good", 0, fn)],
        max_ticks=1, sleep=lambda _s: None)
    assert seen == ["ran"]


def test_a_job_that_raises_still_runs_again_tomorrow():
    calls: list[int] = []

    def flaky():
        calls.append(1)
        raise RuntimeError("again")

    run([Job("flaky", every_seconds=0, fn=flaky)], max_ticks=3,
        sleep=lambda _s: None)
    assert len(calls) == 3, "one failure retired the job"


def test_the_api_does_not_start_scheduled_work_by_itself(monkeypatch):
    """A long job inside the API is a way for the site to go down.

    Reading the lifespan rather than starting the app: what matters is that the
    daily pass is behind a switch that is off, not that a test can boot FastAPI.
    """
    import inspect

    import app.main as main

    src = inspect.getsource(main.lifespan)
    assert "PORTALS_IN_API" in src, (
        "the API starts the daily portal pass unconditionally again"
    )
    i = src.index("start_daily_portals()")
    assert "PORTALS_IN_API" in src[:i], "the switch is not what guards the start"


def test_the_worker_knows_about_the_daily_portal_pass():
    """The one job it exists for so far."""
    from app.worker import build_jobs

    assert [j.name for j in build_jobs()] == ["daily portal pass"]
