"""Running one Apify actor and reading its answer.

Trade Me, OneRoof and realestate.co.nz render their figures client-side, so
reading them needs a real browser. Apify runs the browser; this is the client.

One call, synchronous: start the actor, wait for it, read the dataset. Actors at
this size finish in tens of seconds, and the caller is a background worker with
a per-property budget, so waiting is simpler than a callback and there is
nothing to reconcile if the process restarts.

Everything here is best-effort. A portal that is down, an actor that changed its
output, a token that has expired — each returns nothing and the property keeps
whatever it already had. A missing estimate is a blank tile; a raised exception
in the middle of a batch is thirty properties that never got looked at.
"""

from __future__ import annotations

import re
import time

import httpx

from ..config import settings

_BASE = "https://api.apify.com/v2"

# An actor that has not finished in this long is not going to be useful for a
# batch of thirty. The run keeps going on their side; we stop waiting.
RUN_TIMEOUT = 180.0
POLL_EVERY = 3.0
HTTP_TIMEOUT = 30.0


class ApifyUnavailable(RuntimeError):
    """No token configured, or Apify itself could not be reached."""


def configured() -> bool:
    return bool(getattr(settings, "apify_token", ""))


def run_actor(actor: str, payload: dict, *, timeout: float = RUN_TIMEOUT,
              limit: int = 10) -> list[dict]:
    """Run `actor` with `payload` and return up to `limit` dataset items.

    `actor` is the store name with the slash replaced — "solidcode/oneroof" is
    passed as "solidcode~oneroof", which is what the REST API expects.
    """
    token = getattr(settings, "apify_token", "")
    if not token:
        raise ApifyUnavailable("no APIFY_TOKEN configured")

    actor_path = actor.replace("/", "~")
    started = time.monotonic()
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            run = client.post(
                f"{_BASE}/acts/{actor_path}/runs",
                params={"token": token},
                json=payload,
            )
            if run.status_code >= 400:
                raise ApifyUnavailable(f"actor start returned {run.status_code}")
            data = (run.json() or {}).get("data") or {}
            run_id = data.get("id")
            dataset_id = data.get("defaultDatasetId")
            if not run_id:
                raise ApifyUnavailable("actor start returned no run id")

            status = data.get("status")
            while status in ("READY", "RUNNING") and time.monotonic() - started < timeout:
                time.sleep(POLL_EVERY)
                got = client.get(f"{_BASE}/actor-runs/{run_id}",
                                 params={"token": token})
                if got.status_code >= 400:
                    break
                info = (got.json() or {}).get("data") or {}
                status = info.get("status")
                dataset_id = info.get("defaultDatasetId") or dataset_id

            if not dataset_id:
                return []
            # Whatever it managed to collect, even if it did not finish cleanly:
            # a timed-out run that wrote three items is three items we can use.
            items = client.get(f"{_BASE}/datasets/{dataset_id}/items",
                               params={"token": token, "limit": limit,
                                       "clean": "true", "format": "json"})
            if items.status_code >= 400:
                return []
            out = items.json()
            return out if isinstance(out, list) else []
    except ApifyUnavailable:
        raise
    except Exception as e:                       # network, JSON, anything
        raise ApifyUnavailable(f"{type(e).__name__}: {e}") from e


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def num(v) -> float | None:
    """A number out of whatever an actor put in the field.

    Actors are written by different people against different pages, so the same
    idea arrives as 1250000, "1,250,000", "$1.25m", "210 m²" or "".

    Take the FIRST number and read the unit that follows it. The previous
    version deleted every non-digit and parsed what was left, which is wrong in
    both directions:

        "210 m2"  -> "2102"   a 210 m² floor area read as 2,102 m²
        "182m²"   -> "182²"   ValueError, so a real area read as missing
                              (superscript two answers True to isdigit())

    A ten-fold floor area is not a wrong tile, it is a wrong valuation — floor
    area drives the $/m² comp rate — so this parses rather than strips.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    s = str(v).strip().lower().replace(",", "").replace("$", "")
    if not s:
        return None
    m = _NUMBER.search(s)
    if not m:
        return None
    try:
        n = float(m.group())
    except ValueError:
        return None

    # A multiplier counts only when it is the whole of what follows the number:
    # "1.25m" is $1.25 million, "182m²" is 182 square metres.
    tail = s[m.end():].strip()
    if tail == "m":
        n *= 1_000_000
    elif tail == "k":
        n *= 1_000
    elif tail.startswith("ha"):
        n *= 10_000                              # hectares, for rural land areas
    return n or None


def pick(item: dict, *names: str):
    """First present value among `names`, searching one level of nesting.

    Actors disagree about spelling — floorArea, floor_area, floorAreaM2 — and
    some nest the interesting parts under "property" or "attributes".
    """
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    for holder in ("property", "attributes", "details", "estimate", "valuation"):
        nested = item.get(holder)
        if isinstance(nested, dict):
            for name in names:
                if name in nested and nested[name] not in (None, ""):
                    return nested[name]
    return None
