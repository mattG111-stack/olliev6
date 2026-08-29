"""What our customers are hunting, in aggregate — and whether we hold it.

This is the reason the questions are worth asking. One customer's answer
personalises their feed; all of them together say which region to ingest next
and whether the subdivision work is the main event or a side dish.

Counts only. No email, no name, no per-person row — deliberately, and not
merely because a promoter must never see a customer. An admin page that lists
who wants what invites being used as a prospect list; one that reports demand
against supply can only be used to decide what to build.

Aggregating in Python rather than SQL: the lists are JSON text on the user row,
and there are hundreds of users, not millions. When that stops being true this
wants a join table, and the shape of the answer will not change.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import preferences as prefs
from ..db import get_db
from ..models import PropertyForSale, User, UserRole, UserStatus
from ..security import require_admin
from .properties import _active_batch, _hide_bad_data

router = APIRouter(prefix="/api/admin/customer-intel", tags=["admin"])

# A suburb is "thin" when we hold far fewer listings than the number of people
# watching it would justify. Expressed as listings per interested customer, so
# it does not swing with the size of the customer base.
THIN_LISTINGS_PER_WATCHER = 3.0
# Below this many watchers a ratio is noise, not a signal.
MIN_WATCHERS_TO_JUDGE = 3
# How many suburb rows the page shows.
TOP_AREAS = 12


class GoalCount(BaseModel):
    key: str
    label: str
    count: int


class AreaDemand(BaseModel):
    suburb: str
    watchers: int          # people who named this suburb
    listings: int          # what we actually hold there, live and visible
    verdict: str           # "covered" | "thin" | "over_supplied"


class PairCount(BaseModel):
    labels: list[str]
    count: int


class CustomerIntel(BaseModel):
    customers: int              # accounts that could answer
    answered: int               # accounts that have
    answered_pct: float | None
    # Told us something specific, as opposed to answering "just show me
    # everything" — the difference between a response rate and a useful one.
    with_criteria: int
    median_max_price: float | None
    median_min_price: float | None
    changed_at_review: int      # re-stated criteria differing from their first
    goals: list[GoalCount]
    top_pair: PairCount | None
    areas: list[AreaDemand]
    gap_watchers: int           # people watching suburbs we barely cover
    gap_suburbs: list[str]
    generated_at: datetime


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


@router.get("", response_model=CustomerIntel)
def customer_intel(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> CustomerIntel:
    now = datetime.now(timezone.utc)

    # Only people who could have been asked. A promoter sees no listings and is
    # never shown the questions, so counting them would understate the response
    # rate against a denominator that was never in the room.
    customers = (
        db.query(User)
        .filter(User.role == UserRole.USER.value,
                User.status != UserStatus.DEACTIVATED.value)
        .all()
    )

    answered = 0
    with_criteria = 0
    changed = 0
    goal_tally: dict[str, int] = {k: 0 for k, _lbl in prefs.GOALS}
    pair_tally: dict[tuple[str, ...], int] = {}
    watchers: dict[str, int] = {}
    max_prices: list[float] = []
    min_prices: list[float] = []

    for u in customers:
        if not prefs.is_set(u):
            continue
        answered += 1
        s = prefs.summary(u)
        if s["goals"] or s["suburbs"] or s["districts"] or s["max_price"]:
            with_criteria += 1
        for g in s["goals"]:
            goal_tally[g] = goal_tally.get(g, 0) + 1
        if len(s["goals"]) >= 2:
            key = tuple(s["goals"][:2])
            pair_tally[key] = pair_tally.get(key, 0) + 1
        for name in s["suburbs"]:
            clean = name.strip()
            if clean:
                watchers[clean] = watchers.get(clean, 0) + 1
        if s["max_price"] is not None:
            max_prices.append(s["max_price"])
        if s["min_price"] is not None:
            min_prices.append(s["min_price"])
        # They have come back and re-stated it at least once. set_at never
        # moves; reviewed_at does — so a gap between them is a customer who has
        # been through a check-in, which is what tells us the check-in is worth
        # keeping.
        set_at = getattr(u, "preferences_set_at", None)
        reviewed = getattr(u, "preferences_reviewed_at", None)
        if set_at is not None and reviewed is not None:
            if set_at.tzinfo is None:
                set_at = set_at.replace(tzinfo=timezone.utc)
            if reviewed.tzinfo is None:
                reviewed = reviewed.replace(tzinfo=timezone.utc)
            if reviewed - set_at > timedelta(minutes=1):
                changed += 1

    # What we hold, counted with the SAME visibility rule the customer's own
    # screens use — otherwise this page reports coverage nobody can see.
    listings: dict[str, int] = {}
    batch_id = _active_batch(db, "for_sale", "Auckland")
    if batch_id is not None:
        rows = (_hide_bad_data(
            db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch_id))
            .with_entities(PropertyForSale.suburb)
            .filter(PropertyForSale.suburb.isnot(None)).all())
        for (s,) in rows:
            name = (s or "").strip()
            if name:
                listings[name] = listings.get(name, 0) + 1

    # Match a watched suburb to what we hold case-insensitively — "Mt Wellington"
    # typed by a person and "Mount Wellington" in the feed are still two things,
    # but a capital letter should not invent a coverage gap.
    held_by_key = {k.casefold(): v for k, v in listings.items()}

    areas: list[AreaDemand] = []
    gap_suburbs: list[str] = []
    gap_watchers = 0
    for name, count in sorted(watchers.items(), key=lambda kv: (-kv[1], kv[0])):
        held = held_by_key.get(name.casefold(), 0)
        if count < MIN_WATCHERS_TO_JUDGE:
            verdict = "covered"
        elif held < count * THIN_LISTINGS_PER_WATCHER:
            verdict = "thin"
            gap_suburbs.append(name)
            gap_watchers += count
        elif held > count * THIN_LISTINGS_PER_WATCHER * 4:
            verdict = "over_supplied"
        else:
            verdict = "covered"
        areas.append(AreaDemand(suburb=name, watchers=count,
                                listings=held, verdict=verdict))

    top_pair = None
    if pair_tally:
        key, n = max(pair_tally.items(), key=lambda kv: kv[1])
        top_pair = PairCount(labels=[prefs.GOAL_LABELS.get(k, k) for k in key], count=n)

    return CustomerIntel(
        customers=len(customers),
        answered=answered,
        answered_pct=(100.0 * answered / len(customers)) if customers else None,
        with_criteria=with_criteria,
        median_max_price=_median(max_prices),
        median_min_price=_median(min_prices),
        changed_at_review=changed,
        goals=[GoalCount(key=k, label=lbl, count=goal_tally.get(k, 0))
               for k, lbl in prefs.GOALS],
        top_pair=top_pair,
        areas=areas[:TOP_AREAS],
        gap_watchers=gap_watchers,
        gap_suburbs=gap_suburbs[:6],
        generated_at=now,
    )
