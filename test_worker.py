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


def test_the_worker_owns_every_heavy_job():
    """Everything that is long, memory-hungry, or reaches somebody else's
    servers belongs here rather than in the container answering requests."""
    from app.worker import build_jobs

    names = [j.name for j in build_jobs()]
    assert "daily portal pass" in names
    assert "new listings sweep" in names
    assert "sold sweep" in names
    assert "re-price stale batches" in names


def test_the_re_price_self_heal_is_not_started_by_the_api(monkeypatch):
    """It re-prices a staged batch — the whole sold history plus every listing
    through pandas — and it was doing that on EVERY API boot. The app started,
    reported ready, served, and was then OOM-killed mid-pandas, leaving a
    CancelledError in the lifespan that named nothing. It killed 8.997."""
    import pathlib as _p

    src = _p.Path("app/main.py").read_text()
    assert "auto_reprice_stale_batches" in src
    i = src.index("threading.Thread(target=auto_reprice_stale_batches")
    guard = src[:i]
    assert "AUTO_REPRICE_ON_BOOT" in guard, (
        "the self-heal is started unconditionally on boot again")


def test_the_sold_sweep_runs_a_seventh_as_often_as_the_listings_one():
    """Sales weekly, listings daily. The cadence is most of the cost."""
    from app.worker import build_jobs

    by = {j.name: j.every for j in build_jobs()}
    assert by["sold sweep"] == 7 * by["new listings sweep"]
