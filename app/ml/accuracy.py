"""How close are we, against Hougarden and against the council figure.

The only comparison that means anything is against what a house ACTUALLY SOLD
FOR. Two estimates of a property that has not sold can disagree by 20% and
neither is wrong yet — that measures disagreement, not accuracy.

So this is measured on sold records, where three numbers sit in the same row:

    sale_price                  what it actually went for
    third_party_valuation       Hougarden's estimate  (hg_valuation in the DB)
    cv_numeric                  the council valuation

and a fourth we have to compute honestly: ours.

WHY OURS HAS TO BE COMPUTED RATHER THAN READ

There is no "our valuation" column on a sold row, and that is a good thing. If
there were, it would have been written after the sale — with the sale itself in
the comp set — and it would score beautifully and mean nothing.

Ours is computed FORWARD: split the sales by date, fit on the earlier ones,
value the later ones. Every one of our numbers is produced by an engine that
had never seen that sale, or any sale after it. That is a harder test than
either of the numbers we are compared against faces here, and it is the only
one that predicts how we do next month.

WHY THE COMPARISON MUST BE ON THE SAME ROWS

Hougarden does not carry an estimate for every property. If we score ourselves
on 40,000 sales and them on the 12,000 they happen to cover, we are comparing
two different markets and the number is worthless — and probably flattering,
because the properties a portal is confident enough to estimate are the ordinary
well-traded ones every method prices well.

Every figure here is computed on the SAME rows: the ones where all three numbers
exist. `n` is reported beside every cell, and a cell too thin to mean anything
reports None rather than a number.

MAPE, and why the median sits next to it

MAPE — mean absolute percentage error — is the number people ask for, so it is
what is reported. It is also badly behaved on property: one sale recorded at a
tenth of its real price (a family transfer, a mis-keyed record) moves the mean
of 500 by a full point, and property data is full of those. The median absolute
percentage error is beside it for that reason. When the two disagree sharply,
the mean is being dragged by a handful of broken records, and the median is the
one to believe.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd

from ..pricing.glm import canonical_type
from . import features as F
from .train import fit

# The types worth reporting separately. A blended figure across everything
# hides that a method can be good at houses and poor at apartments — which is
# the usual shape, because an apartment's value is driven by the building and
# the floor it is on, and no method here can see either.
TYPES = ("House", "Townhouse", "Apartment")

# Below this a percentage error is one property's story, not a measurement.
MIN_ROWS = 30

# A sale outside this against its own CV is a broken record, not evidence.
# Leaving them in would flatter whichever method happens to be closest to a
# number that is itself wrong.
SANE = (0.3, 3.0)

# Fit on the oldest share, measure on the rest.
TRAIN_FRACTION = 0.7


@dataclass
class Cell:
    """One method, one property type."""
    n: int = 0
    mape: float | None = None            # mean absolute % error
    median: float | None = None          # median absolute % error
    within_10: float | None = None       # share priced within 10%

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TypeRow:
    property_type: str
    n: int
    ours: Cell = field(default_factory=Cell)
    hougarden: Cell = field(default_factory=Cell)
    council: Cell = field(default_factory=Cell)

    def as_dict(self) -> dict:
        return {"property_type": self.property_type, "n": self.n,
                "ours": self.ours.as_dict(),
                "hougarden": self.hougarden.as_dict(),
                "council": self.council.as_dict()}


def _hg(df: pd.DataFrame) -> pd.Series:
    """Hougarden's estimate: their own figure, else the middle of their range.

    Both are carried. The point estimate is the one they publish; the range is
    what a listing shows when they will not commit to a point, and its midpoint
    is the fairest single number to hold them to.
    """
    mid = pd.to_numeric(df.get("third_party_valuation"), errors="coerce") \
        if "third_party_valuation" in df.columns else pd.Series(np.nan, index=df.index)
    lo = pd.to_numeric(df.get("third_party_valuation_low"), errors="coerce") \
        if "third_party_valuation_low" in df.columns else pd.Series(np.nan, index=df.index)
    hi = pd.to_numeric(df.get("third_party_valuation_high"), errors="coerce") \
        if "third_party_valuation_high" in df.columns else pd.Series(np.nan, index=df.index)
    band = (lo + hi) / 2.0
    return mid.where(mid.notna() & (mid > 0), band)


def _cell(pred: pd.Series, actual: pd.Series) -> Cell:
    err = ((pred - actual).abs() / actual).replace([np.inf, -np.inf], np.nan).dropna()
    if len(err) < MIN_ROWS:
        return Cell(n=int(len(err)))
    return Cell(n=int(len(err)),
                mape=round(float(err.mean() * 100), 2),
                median=round(float(err.median() * 100), 2),
                within_10=round(float((err <= 0.10).mean() * 100), 1))


def compare(sold: pd.DataFrame) -> dict:
    """Ours vs Hougarden vs the council figure, by property type.

    Returns a plain dict ready to serialise. Everything it cannot measure comes
    back as None with the row count that made it impossible, never as a figure
    with a caveat somewhere else on the page.
    """
    if sold is None or len(sold) == 0:
        return {"rows": [], "overall": None, "reason": "no sold data loaded"}

    df = sold.copy()
    price = F.col(df, "price")
    cv = F.col(df, "cv")
    hg = _hg(df)

    ratio = price / cv.replace(0, np.nan)
    ok = (price.notna() & (price > F.MIN_PRICE) & cv.notna() & (cv > 0)
          & ratio.between(*SANE))
    # THE fairness rule: only rows where all three have something to say. Scoring
    # ourselves on every sale and them on the subset they cover compares two
    # different markets.
    ok = ok & hg.notna() & (hg > 0)
    df = df[ok].copy()
    if len(df) < MIN_ROWS * 2:
        return {"rows": [], "overall": None,
                "reason": (f"only {len(df)} sales carry an actual price, a "
                           f"council valuation AND a Hougarden estimate — not "
                           f"enough to compare on")}

    months = F.months(df)
    if months.notna().sum() < MIN_ROWS * 2:
        return {"rows": [], "overall": None,
                "reason": "sold records carry no usable dates, so ours cannot "
                          "be measured forward and the comparison would flatter us"}

    cut = float(months.quantile(TRAIN_FRACTION))
    train = df[months <= cut]
    test = df[months > cut].copy()
    if len(train) < 400 or len(test) < MIN_ROWS:
        return {"rows": [], "overall": None,
                "reason": (f"not enough history to fit on and still have sales "
                           f"left to test against ({len(train)} / {len(test)})")}

    try:
        model = fit(train)
        ours = model.predict(test)
    except Exception as exc:                              # noqa: BLE001
        return {"rows": [], "overall": None,
                "reason": f"could not fit a valuation to measure: {exc}"}

    test = test.loc[test.index.intersection(ours.index)]
    ours = ours.reindex(test.index)
    actual = F.col(test, "price")
    council = F.col(test, "cv")
    houga = _hg(test)
    ctype = (test["property_type"].map(canonical_type) if "property_type" in test
             else pd.Series("House", index=test.index))

    def row(label: str, mask: pd.Series) -> TypeRow:
        m = mask & actual.notna()
        return TypeRow(property_type=label, n=int(m.sum()),
                       ours=_cell(ours[m], actual[m]),
                       hougarden=_cell(houga[m], actual[m]),
                       council=_cell(council[m], actual[m]))

    rows = [row(t, ctype == t) for t in TYPES]
    rows = [r for r in rows if r.n > 0]
    overall = row("All", pd.Series(True, index=test.index))

    return {
        "rows": [r.as_dict() for r in rows],
        "overall": overall.as_dict(),
        "tested_from_month": cut,
        "trained_on": int(len(train)),
        "min_rows": MIN_ROWS,
        "reason": None,
        "method": ("Measured against what these homes actually sold for. Our "
                   "figure is produced forward — fitted only on sales that "
                   "happened BEFORE each one — so it has never seen the sale "
                   "it is being scored on. All three are measured on the same "
                   "properties."),
    }
