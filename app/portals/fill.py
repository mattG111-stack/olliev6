"""Applying a portal's answer to one of our listings.

Two rules, and the whole feature rests on them.

  A FACT fills a field we are missing, and never overwrites one we hold. Our own
  feed carries things no portal has and has already been priced off; a second
  opinion about a floor area we already know is not an improvement, it is a
  chance to be wrong in a new way.

  An ESTIMATE is stored in that portal's own columns and shown as theirs. It
  never reaches fair_value, buy_price, a margin or a deal flag. The Trade Me
  export is the standing lesson: their published estimate for a sold property
  turned out to be that property's own sale price indexed forward, and lands a
  median 1.2% from it. Feed a number like that back in and the system confirms
  itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ESTIMATE_COLUMNS, PortalResult

# Portal field → our column. Facts only; estimates are handled separately.
FACTS = {
    "floor_area_m2": "floor_area_m2",
    "land_area_m2": "land_area_m2",
    "beds": "beds",
    "baths": "baths",
    "cars": "cars",
    "cv_numeric": "cv_numeric",
    "land_value_numeric": "land_value_numeric",
    "improvement_value_numeric": "improvement_value_numeric",
    "year_built": "building_age",
    "property_type": "property_type",
    "image_url": "image_url",
}

# Nothing outside these bounds is a floor area, a land area or a valuation. A
# portal that returns a page number, a listing id or a phone number in the wrong
# field would otherwise write it into a priced attribute.
BOUNDS = {
    "floor_area_m2": (10.0, 2_000.0),
    "land_area_m2": (10.0, 500_000.0),
    "beds": (0.0, 20.0),
    "baths": (0.0, 20.0),
    "cars": (0.0, 20.0),
    "cv_numeric": (50_000.0, 100_000_000.0),
    "land_value_numeric": (10_000.0, 100_000_000.0),
    "improvement_value_numeric": (1_000.0, 100_000_000.0),
    "building_age": (1830.0, 2100.0),
    "estimate": (50_000.0, 100_000_000.0),
}


# Filling one of these changes what the property is WORTH, so the listing has to
# be re-priced and its hold re-evaluated afterwards. Filling an image or a
# property type does not. See runner.run_portal_job.
PRICED_FIELDS = frozenset({
    "floor_area_m2", "land_area_m2", "beds", "baths", "cars", "cv_numeric",
    "land_value_numeric", "improvement_value_numeric", "building_age",
})


@dataclass
class Applied:
    """What one property took from one portal."""
    filled: list[str] = field(default_factory=list)
    estimate: bool = False

    def __bool__(self) -> bool:
        return bool(self.filled or self.estimate)

    @property
    def changes_price(self) -> bool:
        return any(f in PRICED_FIELDS for f in self.filled)


def _missing(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:          # NaN
        return True
    return isinstance(v, str) and not v.strip()


def _sane(column: str, value) -> bool:
    lo, hi = BOUNDS.get(column, (None, None))
    if lo is None:
        return True
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return lo <= v <= hi


def apply(prop, res: PortalResult, *, dry_run: bool = False) -> Applied:
    """Fill what is missing, store their estimate, change nothing else."""
    out = Applied()
    if res is None:
        return out

    for src, column in FACTS.items():
        value = getattr(res, src, None)
        if _missing(value) or not hasattr(prop, column):
            continue
        if not _missing(getattr(prop, column)):
            continue                              # ours stands
        if not _sane(column, value):
            continue
        if not dry_run:
            setattr(prop, column, value)
        out.filled.append(column)

    cols = ESTIMATE_COLUMNS.get(res.source)
    if cols and res.estimate is not None and _sane("estimate", res.estimate):
        mid, low, high, url = cols
        # Refreshed rather than filled: a portal's estimate is a current figure
        # and moves with their index, so the newest answer is the right one.
        # This writes ONLY into that portal's own columns.
        if not dry_run:
            setattr(prop, mid, float(res.estimate))
            if low and res.estimate_low is not None:
                setattr(prop, low, float(res.estimate_low))
            if high and res.estimate_high is not None:
                setattr(prop, high, float(res.estimate_high))
            if url and res.url and hasattr(prop, url) and _missing(getattr(prop, url)):
                setattr(prop, url, str(res.url)[:300])
        out.estimate = True

    return out
