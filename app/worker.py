"""Scheduled work, in its own process.

Same codebase, same models, same rules — a different process.

The split is not tidiness. The API's one job is to answer requests, and the work
that runs on a schedule is the opposite shape: long, memory-hungry, and reaching
out to other people's servers. Sharing a container means every one of those is a
way for the site to go down. It already has:

    "a lightweight scan ... keeps memory flat instead of holding 11k objects for
     30 minutes — which is what let a few stacked runs OOM-kill the container"

That comment is in staged_stages.py because it happened. An enrich stage
competing with request serving took the whole thing out — and a container that
dies takes the API with it whether or not the API caused it.

So: heavy and scheduled runs here. If this process OOMs, crashes, or hangs on a
portal that never answers, the site stays up and someone can still sign in and
look at it. If the API restarts, nothing scheduled is lost.

One codebase, though, and deliberately. The models, the pricing, the hold rules
and the tests are shared — two apps would be two definitions of what a property
is, drifting apart one deploy at a time.

Run it as a second service on the same image:

    python -m app.worker

It needs the same DATABASE_URL and the same tokens as the API. Nothing else.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

log = logging.getLogger("worker")

# Checked this often; each job decides for itself whether it is due. A minute is
# short enough that a daily job lands near its hour and long enough to cost
# nothing.
TICK_SECONDS = 60

_stop = False


def _handle_stop(signum, _frame):
    """Finish the tick and exit, so a deploy does not kill a job mid-write."""
    global _stop
    _stop = True
    log.warning("worker: signal %s — stopping after this tick", signum)


class Job:
    """Something that runs on a schedule, and knows when it last ran."""

    def __init__(self, name: str, every_seconds: float, fn, *, enabled=lambda: True):
        self.name = name
        self.every = every_seconds
        self.fn = fn
        self.enabled = enabled
        self.last_run: float | None = None

    def due(self, now: float) -> bool:
        if not self.enabled():
            return False
        return self.last_run is None or (now - self.last_run) >= self.every

    def run(self) -> None:
        """Never raises. A job that fails must not take the worker with it —
        the next one still has to run, and so does this one tomorrow."""
        self.last_run = time.monotonic()
        started = datetime.now(timezone.utc)
        log.warning("worker: %s starting (%s)", self.name,
                    started.isoformat(timespec="seconds"))
        try:
            self.fn()
        except Exception:                          # noqa: BLE001
            log.exception("worker: %s failed", self.name)
        else:
            took = (datetime.now(timezone.utc) - started).total_seconds()
            log.warning("worker: %s finished in %.0fs", self.name, took)


def build_jobs() -> list[Job]:
    """Everything this process is responsible for."""
    from .portals.daily import enabled as portals_enabled
    from .portals.daily import run_once as portals_run
    from .portals.daily import sweep_new_listings, sweep_sold_listings
    from .staged_stages import auto_reprice_stale_batches

    return [
        # New listings daily, sales weekly. See app/portals/listings.py for why
        # those cadences and what each costs.
        Job("new listings sweep", 24 * 60 * 60, sweep_new_listings,
            enabled=portals_enabled),
        Job("sold sweep", 7 * 24 * 60 * 60, sweep_sold_listings,
            enabled=portals_enabled),
        Job("daily portal pass", 24 * 60 * 60, portals_run,
            enabled=portals_enabled),
        # The self-heal that used to run on every API boot, moved here.
        #
        # It re-prices a staged batch: the whole sold history plus every listing
        # through pandas and the pricing pipeline. In the API container that is a
        # memory spike beside the process answering requests, and it killed a
        # deploy on 8.997 — the app started, served, and was then OOM-killed
        # mid-pandas, leaving a CancelledError in the lifespan that named
        # nothing. Here it has the container to itself and a restart costs
        # nobody a page.
        #
        # Hourly rather than daily: it is a no-op unless a batch is actually
        # stale, and when one is, waiting a day to fix stored numbers is too
        # long.
        Job("re-price stale batches", 60 * 60, auto_reprice_stale_batches),
    ]


def run(jobs: list[Job] | None = None, *, tick: float = TICK_SECONDS,
        max_ticks: int | None = None, sleep=time.sleep) -> int:
    """The loop. `max_ticks` and `sleep` exist so a test can run it."""
    jobs = build_jobs() if jobs is None else jobs
    on = [j.name for j in jobs if j.enabled()]
    log.warning("worker: started with %d job(s); enabled: %s",
                len(jobs), ", ".join(on) if on else "none")

    # A restart should not fire everything at once — a crash loop would then be a
    # crash loop that also spends money. Each job waits out one interval first,
    # except on the very first tick after a long gap, which is what the schedule
    # is for anyway.
    ticks = 0
    while not _stop and (max_ticks is None or ticks < max_ticks):
        now = time.monotonic()
        for job in jobs:
            if _stop:
                break
            if job.due(now):
                job.run()
        ticks += 1
        if max_ticks is None or ticks < max_ticks:
            sleep(tick)
    log.warning("worker: stopped after %d tick(s)", ticks)
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    return run()


if __name__ == "__main__":
    sys.exit(main())
