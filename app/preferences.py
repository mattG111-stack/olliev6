"""What a customer is hunting for — one definition, every caller.

Asked inside Ollie rather than at signup: nobody is stopped at the door. The
first time someone opens Ollie he asks what to look for, and every fortnight he
asks whether that still holds. The answers hang off the User record, so they
survive without the customer having to remember to save a search.

This module owns the vocabulary and the clock. Three callers read it — the
preferences API, the Ask page's gate, and the admin aggregate — and a rule that
lives in one of them drifts from the other two, which is how this codebase has
produced disagreeing numbers before.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

# The four things people actually come here for. Keys are stored; labels are
# only ever read by the admin page — the customer-facing copy lives in the
# translation file, because it has to say the same thing in six languages.
GOALS: tuple[tuple[str, str], ...] = (
    ("underpriced", "Priced under the market"),
    ("subdividable", "Land I can split"),
    ("cashflow", "Rent that covers the loan"),
    ("live_in", "Somewhere to live"),
)
GOAL_KEYS = frozenset(k for k, _ in GOALS)
GOAL_LABELS = dict(GOALS)

# How long a stated preference is taken to still be true. "At least every two
# weeks" — so this is a ceiling on staleness, not a reminder interval: the ask
# happens the next time they open Ollie after the fortnight is up, never as an
# interruption to something else.
REVIEW_AFTER_DAYS = 14
# What "ask me again in a month" buys them.
SNOOZE_DAYS = 30

# Guard rails on what can be stored. A budget is a real number a person typed,
# not an opportunity to write 1e308 into the column.
MAX_PRICE = 100_000_000.0
MAX_AREAS = 25
MAX_BEDS = 10


def _aware(dt: datetime | None) -> datetime | None:
    """Postgres hands back tz-aware datetimes and SQLite naive ones.

    Comparing the two raises, which would turn "should we ask?" into a 500 on
    the test database only — the exact shape of bug that shipped in the enrich
    liveness check.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_list(raw: str | None) -> list[str]:
    """Read a stored list back.

    Stored as JSON so a suburb containing a comma survives. Falls back to
    comma-splitting so a value written by hand, or by an earlier build, still
    reads rather than blowing up.
    """
    if not raw:
        return []
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except (ValueError, TypeError):
            loaded = None
        if isinstance(loaded, list):
            return [str(v).strip() for v in loaded if str(v).strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


def dump_list(values) -> str | None:
    """Store a list, de-duplicated, order preserved, capped."""
    if not values:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        s = str(v).strip()
        if not s:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:120])
        if len(out) >= MAX_AREAS:
            break
    return json.dumps(out) if out else None


def clean_list(values) -> list[str]:
    """De-duplicate, trim and cap a list without storing it — same rules as
    dump_list, so what a preview counts is what a save would have stored."""
    return parse_list(dump_list(values))


def clean_goals(values) -> list[str]:
    """Keep only goals we actually know how to act on, in the canonical order."""
    if not values:
        return []
    wanted = {str(v).strip().lower() for v in values}
    return [k for k, _ in GOALS if k in wanted]


def clean_price(value) -> float | None:
    """A price, or None. NaN and infinity are neither — and NaN is truthy."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if f <= 0:
        return None
    return min(f, MAX_PRICE)


def clean_beds(value) -> int | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if f < 1:
        return None
    return int(min(f, MAX_BEDS))


def is_set(user) -> bool:
    """Has this person ever told us what they're after?

    Answering "skip" counts as an answer — we stamp the clock but store no
    criteria, so they are not asked again tomorrow.
    """
    return _aware(getattr(user, "preferences_reviewed_at", None)) is not None


def review_due_at(user) -> datetime | None:
    """When we may next ask. None once we never need to ask again (we always do)."""
    reviewed = _aware(getattr(user, "preferences_reviewed_at", None))
    if reviewed is None:
        return None
    due = reviewed + timedelta(days=REVIEW_AFTER_DAYS)
    snoozed = _aware(getattr(user, "preferences_snoozed_until", None))
    if snoozed is not None and snoozed > due:
        return snoozed
    return due


def state(user, now: datetime | None = None) -> str:
    """"unset" — never asked. "due" — time to check. "current" — leave them alone."""
    now = now or datetime.now(timezone.utc)
    if not is_set(user):
        return "unset"
    due = review_due_at(user)
    if due is not None and due <= now:
        return "due"
    return "current"


def summary(user) -> dict:
    """The stored profile, in the shape both the API and the aggregate read."""
    return {
        "goals": clean_goals(parse_list(getattr(user, "hunt_goals", None))),
        "suburbs": parse_list(getattr(user, "hunt_suburbs", None)),
        "districts": parse_list(getattr(user, "hunt_districts", None)),
        "min_price": clean_price(getattr(user, "hunt_min_price", None)),
        "max_price": clean_price(getattr(user, "hunt_max_price", None)),
        "min_beds": clean_beds(getattr(user, "hunt_min_beds", None)),
    }


def apply(user, *, goals=None, suburbs=None, districts=None,
          min_price=None, max_price=None, min_beds=None,
          now: datetime | None = None) -> None:
    """Write a stated preference onto the user and stamp the clock.

    Saving IS confirming — someone who has just told us what they want should
    not be asked again a moment later, so both timestamps move and any snooze
    is cleared.
    """
    now = now or datetime.now(timezone.utc)
    lo = clean_price(min_price)
    hi = clean_price(max_price)
    # A range typed backwards is a slip, not a request for no results.
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo

    user.hunt_goals = dump_list(clean_goals(goals))
    user.hunt_suburbs = dump_list(suburbs)
    user.hunt_districts = dump_list(districts)
    user.hunt_min_price = lo
    user.hunt_max_price = hi
    user.hunt_min_beds = clean_beds(min_beds)
    if getattr(user, "preferences_set_at", None) is None:
        user.preferences_set_at = now
    user.preferences_reviewed_at = now
    user.preferences_snoozed_until = None


def confirm(user, now: datetime | None = None) -> None:
    """"Nothing's changed" — the criteria stand, the clock restarts."""
    now = now or datetime.now(timezone.utc)
    user.preferences_reviewed_at = now
    user.preferences_snoozed_until = None


def snooze(user, now: datetime | None = None, days: int = SNOOZE_DAYS) -> datetime:
    """"Ask me again in a month." Still an answer; still stamps the clock."""
    now = now or datetime.now(timezone.utc)
    user.preferences_reviewed_at = now
    until = now + timedelta(days=max(1, int(days)))
    user.preferences_snoozed_until = until
    return until
