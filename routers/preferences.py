"""What each customer is hunting — read, write, and the fortnightly check.

The questions are asked inside Ollie, not at signup, so this is the API behind a
conversation rather than behind a form. Three things matter here:

  * The counts we quote back ("571 in that range", "9 fit you this morning")
    come from the SAME visibility rule as the listing pages — `_hide_bad_data`
    and the active batch. Quoting a number the customer then cannot find is
    worse than quoting none.
  * Every stored value goes through app/preferences.py. Nothing writes these
    columns directly.
  * A promoter has no listings and no preferences. This router is behind
    require_active like the rest of the product.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import preferences as prefs
from ..db import get_db
from ..models import BUDGET_PRICE, PropertyForSale, User
from ..security import require_active
from .properties import _active_batch, _area_values, _hide_bad_data

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/preferences", tags=["preferences"])

# How many suburb options the chooser offers. Enough to cover where people
# actually buy, short enough that the list is scannable rather than a phone book.
SUBURB_CHOICES = 40
# Ollie's opening answer shows the best few, not the whole batch.
PREVIEW_ROWS = 3


# ---------- schemas ----------
class PreferencesIn(BaseModel):
    goals: list[str] = Field(default_factory=list)
    suburbs: list[str] = Field(default_factory=list)
    districts: list[str] = Field(default_factory=list)
    min_price: float | None = None
    max_price: float | None = None
    min_beds: int | None = None


class PreferencesOut(BaseModel):
    goals: list[str]
    suburbs: list[str]
    districts: list[str]
    min_price: float | None
    max_price: float | None
    min_beds: int | None
    # "unset" — never asked. "due" — a fortnight has passed. "current" — leave
    # them be. The Ask page reads exactly this to decide what to show.
    state: str
    set_at: datetime | None
    reviewed_at: datetime | None
    review_due_at: datetime | None
    review_after_days: int = prefs.REVIEW_AFTER_DAYS


class SuburbOption(BaseModel):
    suburb: str
    count: int


class OptionsOut(BaseModel):
    suburbs: list[SuburbOption]
    districts: list[str]
    # The price shape of the live market, so a budget is chosen against what is
    # actually out there rather than in the dark.
    price_buckets: list[int]
    price_bucket_edges: list[float]
    total: int


class PreviewRow(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    beds: int | None
    asking_price: float | None
    fair_value: float | None
    margin_dollars: float | None
    max_addl_lots: float | None
    is_subdividable: bool | None
    # Whether the site can be split at all — what the lot count beside it means.
    can_subdivide: bool | None = False


class PreviewOut(BaseModel):
    """What the stated criteria are worth today — the payoff for answering."""
    matches: int
    in_budget: int
    subdividable: int
    underpriced: int
    best_margin_dollars: float | None
    rows: list[PreviewRow]


# ---------- helpers ----------
def _batch(db: Session, user: User) -> int | None:
    return _active_batch(db, "for_sale", "Auckland")


def _matching_query(db: Session, batch_id: int, p: dict, *, apply_goals: bool = True):
    """The listings that fit a stated preference.

    Built on `_hide_bad_data` so it can only ever count rows the customer could
    open. Goals are OR-ed, not AND-ed: someone who wants underpriced houses AND
    splittable land wants both kinds shown, not only the rare property that is
    both — treating the list as an AND is how a personalised feed comes back
    empty and looks broken.
    """
    q = _hide_bad_data(
        db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == batch_id))

    names: list[str] = []
    for s in p["suburbs"]:
        names.extend(_area_values(db, PropertyForSale, batch_id, "suburb", s))
    if names:
        q = q.filter(PropertyForSale.suburb.in_(names))

    districts: list[str] = []
    for d in p["districts"]:
        districts.extend(_area_values(db, PropertyForSale, batch_id, "district", d))
    if districts:
        q = q.filter(PropertyForSale.district.in_(districts))

    # The same budget rule as everywhere else on the site (models.BUDGET_PRICE):
    # the vendor's price where they named one, our valuation where they did not.
    # This is the customer's own saved budget, so reading asking_price alone
    # answered "my range" with the fifth of the market that advertises a number.
    if p["min_price"] is not None:
        q = q.filter(BUDGET_PRICE >= p["min_price"])
    if p["max_price"] is not None:
        q = q.filter(BUDGET_PRICE <= p["max_price"])
    if p["min_beds"] is not None:
        q = q.filter(PropertyForSale.beds.isnot(None),
                     PropertyForSale.beds >= p["min_beds"])

    if apply_goals:
        goals = set(p["goals"])
        clauses = []
        if "underpriced" in goals:
            clauses.append(PropertyForSale.is_underpriced.is_(True))
        if "subdividable" in goals:
            # Somebody who says they are looking for development land wants the
            # land, not only the parcels that pencil at today's asking price —
            # and four sites in five have no asking price to pencil against.
            clauses.append(PropertyForSale.can_subdivide.is_(True))
        if "cashflow" in goals:
            clauses.append(PropertyForSale.is_cashflow_positive.is_(True))
        # "live_in" is not a filter — it is a person telling us they are buying a
        # home, which changes what we lead with, not which listings exist.
        if clauses:
            q = q.filter(or_(*clauses))
    return q


def _criteria(body) -> dict:
    """A request body, cleaned to exactly what a save would have stored.

    Shared by preview and by the aggregate, so a count shown before saving and
    the count shown after cannot disagree.
    """
    lo = prefs.clean_price(body.min_price)
    hi = prefs.clean_price(body.max_price)
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return {
        "goals": prefs.clean_goals(body.goals),
        "suburbs": prefs.clean_list(body.suburbs),
        "districts": prefs.clean_list(body.districts),
        "min_price": lo,
        "max_price": hi,
        "min_beds": prefs.clean_beds(body.min_beds),
    }


def _out(user: User) -> PreferencesOut:
    s = prefs.summary(user)
    return PreferencesOut(
        **s,
        state=prefs.state(user),
        set_at=getattr(user, "preferences_set_at", None),
        reviewed_at=getattr(user, "preferences_reviewed_at", None),
        review_due_at=prefs.review_due_at(user),
    )


# ---------- endpoints ----------
@router.get("", response_model=PreferencesOut)
def get_preferences(user: User = Depends(require_active)) -> PreferencesOut:
    """What we think we know, and whether it is time to ask again."""
    return _out(user)


@router.put("", response_model=PreferencesOut)
def put_preferences(
    body: PreferencesIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> PreferencesOut:
    """Store a stated preference. Saving counts as confirming."""
    prefs.apply(
        user,
        goals=body.goals,
        suburbs=body.suburbs,
        districts=body.districts,
        min_price=body.min_price,
        max_price=body.max_price,
        min_beds=body.min_beds,
    )
    db.commit()
    db.refresh(user)
    return _out(user)


@router.post("/confirm", response_model=PreferencesOut)
def confirm_preferences(
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> PreferencesOut:
    """"Nothing's changed." Restarts the fortnight without touching the criteria."""
    prefs.confirm(user)
    db.commit()
    db.refresh(user)
    return _out(user)


@router.post("/snooze", response_model=PreferencesOut)
def snooze_preferences(
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> PreferencesOut:
    """Ask me again in a month — pushes the next check past the fortnight."""
    prefs.snooze(user)
    db.commit()
    db.refresh(user)
    return _out(user)


@router.post("/skip", response_model=PreferencesOut)
def skip_preferences(
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> PreferencesOut:
    """"Just show me everything."

    An answer, not a dodge: it stamps the clock so they are not asked again
    tomorrow, and stores no criteria so nothing is filtered.
    """
    prefs.confirm(user)
    db.commit()
    db.refresh(user)
    return _out(user)


@router.get("/options", response_model=OptionsOut)
def options(
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> OptionsOut:
    """The suburbs on offer, each with what it actually holds today.

    A chooser that shows counts turns a guess into a decision — and it means an
    area with nothing in it can never be picked in silence.
    """
    batch_id = _batch(db, user)
    if batch_id is None:
        return OptionsOut(suburbs=[], districts=[], price_buckets=[],
                          price_bucket_edges=[], total=0)

    base = _hide_bad_data(
        db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == batch_id))

    # Trimmed, so one suburb cannot appear twice because of a trailing space.
    rows = (base.with_entities(PropertyForSale.suburb)
            .filter(PropertyForSale.suburb.isnot(None)).all())
    tally: dict[str, int] = {}
    for (s,) in rows:
        name = (s or "").strip()
        if not name:
            continue
        tally[name] = tally.get(name, 0) + 1
    suburbs = [SuburbOption(suburb=k, count=v)
               for k, v in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
               ][:SUBURB_CHOICES]

    drows = (base.with_entities(PropertyForSale.district)
             .filter(PropertyForSale.district.isnot(None)).distinct().all())
    districts = sorted({(d or "").strip() for (d,) in drows if (d or "").strip()})

    # The distribution the budget slider sits on. Eleven buckets between the 5th
    # and 95th percentile of asking prices, computed in Python — PERCENTILE_CONT
    # is Postgres-only and this has to run under the test database too.
    prices = sorted(
        float(v) for (v,) in base.with_entities(PropertyForSale.asking_price)
        .filter(PropertyForSale.asking_price.isnot(None),
                PropertyForSale.asking_price > 0).all()
        if v is not None and float(v) == float(v)
    )
    buckets: list[int] = []
    edges: list[float] = []
    if len(prices) >= 2:
        lo = prices[max(0, int(len(prices) * 0.05))]
        hi = prices[min(len(prices) - 1, int(len(prices) * 0.95))]
        if hi > lo:
            n = 11
            step = (hi - lo) / n
            edges = [lo + step * i for i in range(n + 1)]
            buckets = [0] * n
            for p in prices:
                if p < lo or p > hi:
                    continue
                i = min(n - 1, int((p - lo) / step))
                buckets[i] += 1

    return OptionsOut(suburbs=suburbs, districts=districts,
                      price_buckets=buckets, price_bucket_edges=edges,
                      total=base.count())


@router.post("/preview", response_model=PreviewOut)
def preview(
    body: PreferencesIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> PreviewOut:
    """What these criteria are worth today, without saving them.

    Drives both the running total while they choose and the answer Ollie opens
    with once they are done.
    """
    batch_id = _batch(db, user)
    if batch_id is None:
        return PreviewOut(matches=0, in_budget=0, subdividable=0, underpriced=0,
                          best_margin_dollars=None, rows=[])

    p = _criteria(body)
    q = _matching_query(db, batch_id, p)
    matches = q.count()
    # Everything in their areas and budget, before their goals narrow it — the
    # "571 of the listings I watch sit in that range" line.
    in_budget = _matching_query(db, batch_id, p, apply_goals=False).count()
    subdividable = q.filter(PropertyForSale.can_subdivide.is_(True)).count()
    underpriced = q.filter(PropertyForSale.is_underpriced.is_(True)).count()

    gap = PropertyForSale.fair_value - PropertyForSale.asking_price
    best = (q.with_entities(func.max(gap))
            .filter(PropertyForSale.fair_value.isnot(None),
                    PropertyForSale.asking_price.isnot(None)).scalar())

    rows = (q.filter(PropertyForSale.fair_value.isnot(None),
                     PropertyForSale.asking_price.isnot(None))
            .order_by(gap.desc()).limit(PREVIEW_ROWS).all())

    return PreviewOut(
        matches=matches,
        in_budget=in_budget,
        subdividable=subdividable,
        underpriced=underpriced,
        best_margin_dollars=float(best) if best is not None else None,
        rows=[
            PreviewRow(
                id=r.id, address=r.address, suburb=r.suburb, beds=r.beds,
                asking_price=r.asking_price, fair_value=r.fair_value,
                margin_dollars=(
                    float(r.fair_value) - float(r.asking_price)
                    if r.fair_value is not None and r.asking_price is not None else None),
                max_addl_lots=r.max_addl_lots,
                is_subdividable=r.is_subdividable,
                can_subdivide=bool(r.can_subdivide),
            ) for r in rows
        ],
    )
