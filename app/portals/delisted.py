"""Is the advertisement still up?

A listing that vanishes from a portal has usually sold. Usually — not always.
It may equally have been withdrawn, expired, relisted under a new URL, or moved
because the portal changed its URL scheme overnight. A dead link cannot tell
those apart, so nothing in this module ever writes the word "sold". That word
belongs to the weekly sold file, which arrives with a price and a date attached.
What this says is narrower and defensible: the advertisement is gone.

Three rules, and each exists because the naive version of this feature is
actively dangerous — it does not get one listing wrong, it gets thousands wrong
at once, silently, and the wrong answers flow straight into the sold comparables
that price everything else on the site.

1. ONLY 404 AND 410 ARE EVIDENCE.
   403 (blocked), 429 (rate limited), a timeout, a connection reset, a 5xx —
   every one of those means WE could not look, not that the listing is gone.
   They are recorded, so an operator can see what happened, and they never
   count toward anything.

2. EVIDENCE ACCUMULATES ACROSS DAYS, NOT WITHIN A RUN.
   One 404 during a portal's deploy window proves nothing. A listing has to be
   missing on GONE_STREAK separate daily passes before its state changes, and
   a single successful fetch resets the count to zero.

3. A RUN THAT LOOKS TOO SUCCESSFUL IS A RUN THAT IS WRONG.
   If more than MAX_GONE_SHARE of one portal's checks come back missing in a
   single pass, the market did not sell overnight — we are blocked, or the URL
   scheme moved. The run is abandoned for that portal and nothing is written.
   This is the guard that turns a catastrophe into a log line.

Reversible by construction: if a link comes back, the count clears and so does
the delisting. Nothing is ever deleted.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from ..models import PortalListing, PropertyForSale

log = logging.getLogger(__name__)

# Two thresholds, because two different questions are being asked and they carry
# very different costs for being wrong.
#
# HIDE_AFTER — how many confirmed-gone checks before the listing stops being
# shown to customers. One. The whole point of this feature is that nobody clicks
# through from our site to a page that is not there, and the cost of being wrong
# is that a live listing is missing for a day and comes back on the next pass.
# That is a far smaller harm than sending a paying customer to a 404.
HIDE_AFTER = 1
# GONE_STREAK — how many before we record that the advertisement is gone as a
# fact about the market. Three, because that is a claim rather than a courtesy,
# and a 404 during a portal's deploy window is not one.
GONE_STREAK = 3

# Above this share of one portal's checks coming back missing in a single pass,
# the run is disbelieved. Real daily churn is a few percent; a third of a
# portal's listings do not disappear between one night and the next.
MAX_GONE_SHARE = 0.35
# Below this many checks the share is meaningless — five listings of which two
# are gone is 40% and entirely normal.
MIN_TO_JUDGE = 20

# Per pass. The point is a slow, steady sweep, not a stampede at a portal that
# has every reason to start refusing us.
MAX_PER_RUN = 400
PAUSE_SECONDS = (0.6, 1.4)      # jittered, between requests to one host
HTTP_TIMEOUT = 12.0

# Only these mean the page is really gone.
GONE_CODES = {404, 410}


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host or "unknown"
    except Exception:                                  # noqa: BLE001
        return "unknown"


def check_one(client: httpx.Client, url: str) -> tuple[bool, str]:
    """(is_gone, what_happened).

    HEAD first — it is the cheapest way to ask "is this still here", and a
    courtesy to a server we are about to ask four hundred times. Some portals
    do not implement it and answer 405, in which case we ask properly.

    Redirects are followed, and a redirect that LANDS somewhere is not gone:
    portals routinely bounce a sold listing to a "recently sold" page rather
    than 404 it, and that page is a real page. Only the status at the end of
    the chain counts.
    """
    try:
        r = client.head(url)
        if r.status_code == 405:                       # HEAD not supported here
            r = client.get(url)
    except httpx.TimeoutException:
        return False, "timeout"
    except httpx.HTTPError as exc:                     # DNS, reset, TLS, proxy
        return False, type(exc).__name__[:16]
    except Exception as exc:                           # noqa: BLE001
        return False, type(exc).__name__[:16]

    code = r.status_code
    # THE LINE THAT MATTERS. Anything that is not an explicit "this does not
    # exist" is a failure to look, and a failure to look is not evidence.
    return code in GONE_CODES, str(code)


def sweep(db: Session, *, limit: int = MAX_PER_RUN, now: datetime | None = None,
          client: httpx.Client | None = None, sleep=time.sleep) -> dict:
    """One daily pass. Returns what it did, and never raises.

    Checks the listings whose links have gone longest without a look, so the
    sweep rotates through the whole book rather than re-checking the same few.
    """
    now = now or datetime.now(timezone.utc)
    out = {"checked": 0, "gone": 0, "still_up": 0, "unreachable": 0,
           "newly_delisted": 0, "newly_hidden": 0, "came_back": 0,
           "abandoned": []}

    cap = max(1, min(limit, MAX_PER_RUN))
    # THE LIVE TABLE FIRST, and with the larger share of the budget. This is the
    # row a customer actually clicks: a dead link on a staged row nobody can see
    # is a curiosity, a dead link here is a customer landing on a 404 from our
    # own site. Staged rows are checked with what is left over.
    live = (db.query(PropertyForSale)
            .filter(PropertyForSale.url.isnot(None))
            .order_by(PropertyForSale.link_checked_at.asc().nullsfirst(),
                      PropertyForSale.id.asc())
            .limit(cap)
            .all())
    staged = (db.query(PortalListing)
              .filter(PortalListing.url.isnot(None),
                      PortalListing.kind == "for_sale",
                      PortalListing.delisted_at.is_(None))
              .order_by(PortalListing.link_checked_at.asc().nullsfirst(),
                        PortalListing.id.asc())
              .limit(max(0, cap - len(live)))
              .all())
    rows = live + staged
    if not rows:
        return out

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=HTTP_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": "ApexPropertyBot/1.0 (listing availability check)"})

    # Findings are held rather than written as we go: rule 3 cannot be applied
    # until the whole pass is in, and a run that is going to be disbelieved must
    # not have written half its conclusions first.
    findings: list[tuple[PortalListing, bool, str]] = []
    per_host: dict[str, list[int]] = {}

    try:
        for i, row in enumerate(rows):
            gone, what = check_one(client, row.url or "")
            findings.append((row, gone, what))
            h = _host(row.url or "")
            per_host.setdefault(h, [0, 0])
            per_host[h][0] += 1
            if gone:
                per_host[h][1] += 1
            out["checked"] += 1
            if gone:
                out["gone"] += 1
            elif what.isdigit():
                out["still_up"] += 1
            else:
                out["unreachable"] += 1
            if i + 1 < len(rows):
                sleep(random.uniform(*PAUSE_SECONDS))
    finally:
        if owns_client:
            client.close()

    # Rule 3, per portal. A host whose listings have "all sold overnight" is a
    # host that has started refusing us, or has moved its URLs.
    disbelieved = set()
    for h, (total, gone) in per_host.items():
        if total >= MIN_TO_JUDGE and gone / total > MAX_GONE_SHARE:
            disbelieved.add(h)
            out["abandoned"].append({"host": h, "checked": total, "gone": gone})
            log.warning("delisting sweep: %s reported %s of %s listings gone — "
                        "disbelieving this pass", h, gone, total)

    for row, gone, what in findings:
        row.link_checked_at = now
        row.link_last_result = what[:16]
        if _host(row.url or "") in disbelieved:
            continue                                   # looked, learned nothing
        # The staging row records "the advertisement is gone" (a claim about
        # the market, three days). The live row records "do not show this to
        # anybody" (a courtesy, one day). Same evidence, two thresholds.
        is_live = isinstance(row, PropertyForSale)
        threshold = HIDE_AFTER if is_live else GONE_STREAK
        if gone:
            row.link_gone_count = (row.link_gone_count or 0) + 1
            if row.link_gone_count >= threshold:
                if is_live and row.link_dead_at is None:
                    row.link_dead_at = now
                    out["newly_hidden"] += 1
                elif not is_live and row.delisted_at is None:
                    row.delisted_at = now
                    out["newly_delisted"] += 1
        elif what.isdigit():
            # It answered, and not with "gone". Whatever we thought before, the
            # advertisement is up — so it goes back on the site immediately.
            if (row.link_gone_count or 0) > 0:
                out["came_back"] += 1
            row.link_gone_count = 0
            if is_live:
                if row.link_dead_at is not None:
                    row.link_dead_at = None
            elif row.delisted_at is not None:
                row.delisted_at = None
        # An unreachable row keeps whatever streak it had: we learned nothing
        # about it either way, and resetting would let a flaky network hide a
        # listing that really has gone.

    try:
        db.commit()
    except Exception:                                  # noqa: BLE001
        log.exception("could not record the delisting sweep")
        db.rollback()
    return out


def run_once() -> dict:
    """The worker's entry point. Opens its own session and never raises."""
    from ..db import SessionLocal
    from ..runlog import record

    db = SessionLocal()
    try:
        out = sweep(db)
        if out["checked"]:
            record(db, stage="portals", event="links_checked",
                   count=out["checked"],
                   level="warn" if out["abandoned"] else "info",
                   detail=(f"{out['checked']:,} links checked — "
                           f"{out['still_up']:,} still up, {out['gone']:,} gone, "
                           f"{out['unreachable']:,} unreachable, "
                           f"{out['newly_hidden']:,} hidden from the site, "
                           f"{out['newly_delisted']:,} newly off market, "
                           f"{out['came_back']:,} back on"
                           + (f"; DISBELIEVED {len(out['abandoned'])} portal pass(es)"
                              if out["abandoned"] else "")))
        return out
    except Exception:                                  # noqa: BLE001
        log.exception("delisting sweep failed")
        return {"checked": 0, "error": True}
    finally:
        db.close()
