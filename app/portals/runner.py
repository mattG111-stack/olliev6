"""The job behind the button: ask the portals about this week's keepers.

Runs AFTER pricing, over the deal candidates only. That is the whole economy of
it — a weekly file is thousands of listings, of which a few dozen survive the
margin floor, and those are the ones worth spending a lookup on. Held rows are
skipped; each still has its own per-listing button.

Re-runnable. A property that has already been asked about within
PORTAL_TTL_DAYS is skipped, so a run that died at 60% resumes rather than
restarts, and pressing the button twice costs nothing.

Progress is an IngestJob row, the same one the upload and enrich stages use, so
the existing poll and the existing progress bar work unchanged.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from ..db import SessionLocal
from ..models import PropertyForSale
from ..reprice import reprice_one
from ..staged_stages import _update
from . import DEFAULT_ORDER
from .fill import apply
from .harvest import HarvestCache
from .findings import record
from .sources import LOOKUPS

log = logging.getLogger(__name__)

# Asked this recently, do not ask again. Portal estimates move slowly and a
# re-run is usually someone pressing the button twice.
PORTAL_TTL_DAYS = 14
# Courtesy gap between lookups. Thirty properties across five sources is 150
# requests; spread out, that is nothing to anyone.
DELAY = 1.0
# A ceiling, so a mis-click cannot spend an afternoon and a fortune.
DEFAULT_CAP = 200


def _takes_cache(fn) -> bool:
    """Does this lookup accept the shared suburb-harvest cache?

    The real sources all do (the two direct ones swallow it as **_), but the
    tests pass plain two-argument doubles and a caller may too.
    """
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):               # builtins, odd callables
        return False
    return "cache" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _fresh(stamp, days: int) -> bool:
    if not stamp:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamp < timedelta(days=days)


def candidates(db, batch_id: int, *, ttl_days: int = PORTAL_TTL_DAYS) -> list[int]:
    """The keepers we have not asked about lately. Ids only — memory stays flat."""
    scan = (db.query(PropertyForSale.id, PropertyForSale.is_held,
                     PropertyForSale.address, PropertyForSale.portals_checked_at)
            .filter(PropertyForSale.import_batch_id == batch_id)
            .order_by(PropertyForSale.id).all())
    return [r.id for r in scan
            if not r.is_held
            and r.address
            and not _fresh(r.portals_checked_at, ttl_days)]


def run_portal_job(job_id: int, batch_id: int, region: str = "Auckland", *,
                   sources: tuple[str, ...] = DEFAULT_ORDER,
                   cap: int = DEFAULT_CAP, delay: float = DELAY,
                   lookups: dict | None = None, review: bool = True) -> None:
    """Ask every source about every keeper and write down what they said.

    review=True (the default) changes NOTHING. Each answer becomes a pending
    finding for a person to look at — a figure scraped off someone else's page
    is a claim, and it should not enter a priced field without someone having
    seen it. Approving one writes it and re-prices the listing through the usual
    rules; see portals/findings.py.

    review=False applies as it goes, which is only for a caller that has already
    decided to trust the sources.

    `lookups` exists so the tests can run the whole job without a network.
    """
    lookups = lookups or LOOKUPS
    db = SessionLocal()
    try:
        _update(db, job_id, status="running", stage="portals",
                started_at=datetime.now(timezone.utc), progress_pct=0,
                rows_filled=0, rows_missed=0)

        todo = candidates(db, batch_id)[:cap]
        _update(db, job_id, rows_total=len(todo), rows_inserted=0)
        if not todo:
            _update(db, job_id, status="completed", progress_pct=100,
                    completed_at=datetime.now(timezone.utc), stage="done",
                    error_message=None)
            return

        answered = filled = asked = repriced = 0
        by_source: dict[str, int] = {}
        # One harvest per (portal, suburb), reused across every property in that
        # suburb — see portals/harvest.py. Without it the three browser portals
        # would run an actor per property instead of per suburb, which for a
        # batch of thirty across six suburbs is 90 runs instead of 18.
        cache = HarvestCache()

        # Sort by suburb so a harvest is built once and used straight away
        # rather than being rebuilt as the run wanders between suburbs.
        todo.sort(key=lambda pid: (
            (db.get(PropertyForSale, pid).suburb or "").strip().lower(), pid))

        for i, pid in enumerate(todo, start=1):
            prop = db.get(PropertyForSale, pid)
            if prop is None:
                continue
            got_any = False
            repriced_this = False
            for name in sources:
                fn = lookups.get(name)
                if fn is None:
                    continue
                asked += 1
                try:
                    # The suburb-harvest sources share `cache`. Asked by
                    # signature, not by catching TypeError — catching it here
                    # would also swallow a genuine TypeError raised inside the
                    # lookup and report it as "this portal knows nothing".
                    res = (fn(prop.address, prop.suburb, cache=cache)
                           if _takes_cache(fn) else fn(prop.address, prop.suburb))
                except Exception as e:            # noqa: BLE001 — one bad source
                    log.info("%s raised for %s: %s", name, prop.address, e)
                    res = None
                if res is None:
                    continue
                if review:
                    found = record(db, prop, res, batch_id=batch_id)
                    if found:
                        got_any = True
                        filled += len(found)
                        by_source[name] = by_source.get(name, 0) + 1
                else:
                    applied = apply(prop, res)
                    if applied:
                        got_any = True
                        filled += len(applied.filled)
                        by_source[name] = by_source.get(name, 0) + 1
                        repriced_this = repriced_this or applied.changes_price
                if delay:
                    time.sleep(delay)

            # Stamped whatever came back, including nothing. An address no
            # portal recognises should not be retried every single run.
            prop.portals_checked_at = datetime.now(timezone.utc)
            if got_any:
                answered += 1
            db.commit()

            # A filled floor area, land area or CV changes what the property is
            # worth, so it goes back through the same pricing and the same hold
            # rules as everything else. Without this a portal could quietly move
            # a listing across the margin floor and nothing would re-check it —
            # the number on screen would be from before the fill.
            if repriced_this:
                try:
                    reprice_one(db, pid)
                    repriced += 1
                except ValueError:
                    pass                          # nothing to price against yet
                except Exception as e:            # noqa: BLE001
                    log.info("re-price after fill failed for %s: %s", pid, e)

            if i % 5 == 0 or i == len(todo):
                _update(db, job_id, rows_inserted=i, rows_filled=filled,
                        rows_missed=i - answered,
                        progress_pct=int(i / len(todo) * 100))
                db.expunge_all()                  # keep memory flat

        bits = [f"{answered} of {len(todo)} properties answered",
                (f"{filled} finding(s) waiting for you to check" if review
                 else f"{filled} field(s) filled")]
        if repriced:
            bits.append(f"{repriced} re-priced and re-checked against the margin floor")
        if by_source:
            bits.append(" · ".join(f"{k} {v}" for k, v in sorted(by_source.items())))
        _update(db, job_id, status="completed", stage="done", progress_pct=100,
                rows_inserted=len(todo), rows_filled=filled,
                rows_missed=len(todo) - answered,
                completed_at=datetime.now(timezone.utc),
                result_json=json.dumps({"summary": " · ".join(bits),
                                        "answered": answered,
                                        "properties": len(todo),
                                        "fields_filled": filled,
                                        "review": review,
                                        "repriced": repriced,
                                        "by_source": by_source}))
    except Exception as e:                        # noqa: BLE001
        log.exception("portal job failed")
        _update(db, job_id, status="failed", stage="error",
                error_message=f"{type(e).__name__}: {e}",
                completed_at=datetime.now(timezone.utc))
    finally:
        db.close()
