"""Turning a table of sales into a matrix a model can be fitted to.

Everything the valuation knows today came out of a spreadsheet. `v38_tables/`
holds coefficients extracted from `Algo data 17-05-2026.xlsx` — fitted once, by
someone else, on data we never see, and never fitted again. Sold files have
landed every week since; not one of them has moved a single coefficient.

So this is the first half of learning from our own sales: a design matrix built
from the columns we actually hold, with the awkward parts handled honestly
rather than dropped.

THREE THINGS THIS GETS RIGHT THAT A NAIVE VERSION WOULD NOT

Missing values are a signal, not a gap to fill. 1,900 of 10,168 sales in the
committed fixture carry no land area. Dropping them throws away a fifth of the
evidence and biases what is left toward properties whose records are complete —
which is not a random fifth. Each numeric column gets its median in place of the
blank AND a was-missing indicator, so the model can learn what a missing record
implies instead of being lied to about it.

Areas and prices are logged. Property value is multiplicative — a second
bathroom is worth a percentage, not a fixed sum, and the same percentage on a
$700k house and a $3M one. Fitting in log space makes the model linear in the
thing that is actually linear, and it stops the handful of $12M sales from
dominating a fit that has to serve a $900k median.

Time is a feature. The market moves; a model fitted across two years of sales
with no time term quietly reports the average of a market that has moved on.
Months-since-oldest-sale lets the fit carry the trend rather than smear it.

Suburb is deliberately NOT here. Three hundred suburbs as one-hot columns is a
matrix mostly made of zeros, and the thin ones would fit noise. Suburb is
handled in train.py as a shrunk residual effect, which is the same credibility
idea the rest of the codebase already uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..periods import sold_year_month
from ..pricing.glm import canonical_type

# Columns arrive under the scraper's names or the tidy ones depending on who
# built the frame. Same idea as _SOLD_ALIASES in the assistant tools: read both
# rather than making every caller normalise first.
ALIASES: dict[str, tuple[str, ...]] = {
    "price": ("sale_price", "price_numeric"),
    "cv": ("cv_numeric",),
    "land_value": ("land_value_numeric",),
    "floor": ("floor_area_m2", "key_floor_area"),
    "land": ("land_area_m2", "key_land_area"),
    "beds": ("beds", "key_bedrooms"),
    "baths": ("baths", "key_bathrooms"),
    "cars": ("cars", "key_carspaces"),
    "age": ("building_age",),
}

# A sale outside this band against its own council valuation is not evidence
# about the market, it is a broken record — a family transfer, a mis-keyed CV,
# a section sold with the house still on the title. The existing engine already
# excludes these (RATIO_CV_LO/HI in buyprice.py); training on them would bake
# the same broken records into every coefficient.
SANE_CV_RATIO = (0.3, 3.0)
MIN_PRICE = 50_000.0

# Titles sell at genuinely different levels — leasehold and cross-lease trade
# well below freehold for the same house — so they are separate columns rather
# than one number pretending the difference is linear.
TITLE_CLASSES = ("LH", "CL", "UT")


def col(df: pd.DataFrame, name: str) -> pd.Series:
    """A numeric column under whichever spelling this frame uses."""
    for c in ALIASES.get(name, (name,)):
        if c in df.columns:
            return pd.to_numeric(df[c], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype="float64")


def title_bucket(v) -> str:
    """FH / LH / CL / UT from either spelling the two datasets use.

    Sold rows store a numeric code as a string ("1.0"); for-sale rows store the
    word ("Freehold"). Mirrors _title_bucket in buyprice.py — same mapping, kept
    here so the trainer has no reason to import the pricing engine.
    """
    s = str(v).strip().lower()
    if s in ("1", "1.0", "freehold", "fee simple"):
        return "FH"
    if s in ("2", "2.0", "leasehold"):
        return "LH"
    if s in ("3", "3.0", "cross-lease", "cross lease"):
        return "CL"
    if s in ("4", "4.0", "unit title", "unit-title", "stratum"):
        return "UT"
    return "FH"                      # the overwhelming majority, and the base case


def months(df: pd.DataFrame) -> pd.Series:
    """Sale date as a month number, from whichever date column exists.

    sold_date carries two formats in the same column — 5/14/2026 from the
    original scraper and 2026-05-14 from current ingest — so it goes through the
    shared parser rather than a split on "/". A split on "/" does not fail
    loudly on ISO input; it silently yields nothing, and a time term that is
    always NaN is a time term that does nothing at all.
    """
    for c in ("sold_date", "sold_listing_date"):
        if c in df.columns:
            ym = df[c].map(sold_year_month)
            y = pd.to_numeric(ym.map(lambda v: v[0] if v else None), errors="coerce")
            m = pd.to_numeric(ym.map(lambda v: v[1] if v else None), errors="coerce")
            return y * 12 + m
    return pd.Series(np.nan, index=df.index, dtype="float64")


@dataclass
class Design:
    """A fitted-ready matrix plus everything needed to rebuild it identically.

    The imputation medians and the column order are part of the MODEL, not part
    of the training run: predicting on one property later has to fill a blank
    floor area with the same number the fit was told, or the coefficient means
    something different at predict time than it did at fit time. Carrying them
    here is what makes that impossible to get wrong.
    """
    X: np.ndarray
    y: np.ndarray                      # ln(price)
    names: list[str]
    medians: dict[str, float]
    rows: pd.DataFrame                 # the surviving rows, for grouping later
    base_month: float = 0.0
    # The newest month in the training data. Undated rows are valued here.
    latest_month: float = 0.0
    dropped: dict[str, int] = field(default_factory=dict)


def usable(df: pd.DataFrame, training: bool = True) -> pd.Series:
    """Rows this model can work with. The rule is NOT the same both ways.

    TRAINING needs a price, a CV, and a sane ratio between them: a sale at 8x
    its council valuation is a family transfer or a mis-keyed record, and
    training on it bakes that into every coefficient.

    PREDICTING needs only a CV. Applying the training rule here would be a
    quiet disaster in exactly the direction that matters: on a for-sale row
    `price_numeric` is the ASKING price, not a sale price, so the ratio filter
    would refuse to price any listing asking far from its council valuation —
    which is the definition of the underpriced listings this whole product
    exists to find. The model would silently decline to value precisely the
    deals, and every one of them would fall back to the old estimator with
    nothing on screen to say so.
    """
    cv = col(df, "cv")
    has_cv = cv.notna() & (cv > 0)
    if not training:
        return has_cv
    price = col(df, "price")
    ratio = price / cv.replace(0, np.nan)
    return (price.notna() & (price > MIN_PRICE) & has_cv
            & ratio.between(*SANE_CV_RATIO))


def build(df: pd.DataFrame, medians: dict[str, float] | None = None,
          base_month: float | None = None, training: bool = True,
          latest_month: float | None = None) -> Design:
    """The design matrix.

    Pass `medians` and `base_month` from an already-fitted model to build a
    matrix for PREDICTION; leave them out to fit fresh.
    """
    keep = usable(df, training=training)
    rows = df[keep].copy()

    # y is only meaningful when fitting. On a prediction frame `price` may be an
    # asking price or absent entirely; it is built and ignored.
    price = col(rows, "price")
    with np.errstate(divide="ignore", invalid="ignore"):
        y = np.log(price.to_numpy(dtype="float64"))
    y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)

    mon = months(rows)
    if base_month is None:
        base_month = float(mon.min()) if mon.notna().any() else 0.0
    t = mon - base_month
    # A row with no date is valued at the NEWEST month the model was fitted on,
    # not the oldest.
    #
    # A for-sale listing has no sold_date - it has not sold - so `months` is
    # empty for every live row, and filling that with zero valued every listing
    # at the market as it stood at the START of the sold window, up to two years
    # ago. In a market that has moved 10% since, that is a 10% error applied to
    # every listing in the same direction, which is exactly the shape of error
    # that empties a deal feed or floods it.
    #
    # `latest_month` travels with the model for this reason; falling back to the
    # frame's own maximum keeps a fit on undated data working.
    _latest = latest_month if latest_month is not None else (
        float(mon.max()) if mon.notna().any() else base_month)
    t = t.fillna(float(_latest) - base_month)

    cols: dict[str, pd.Series] = {}
    med: dict[str, float] = dict(medians or {})

    def numeric(name: str, series: pd.Series, logged: bool = False,
                shift: float = 0.0):
        """One numeric feature, plus the flag that says it was missing."""
        v = series.copy()
        if logged:
            v = np.log(v.clip(lower=0) + shift)
            v = v.replace([np.inf, -np.inf], np.nan)
        if name not in med:
            med[name] = float(v.median()) if v.notna().any() else 0.0
        cols[f"missing_{name}"] = v.isna().astype("float64")
        cols[name] = v.fillna(med[name]).astype("float64")

    numeric("ln_cv", col(rows, "cv"), logged=True, shift=1.0)
    numeric("ln_floor", col(rows, "floor"), logged=True, shift=1.0)
    numeric("ln_land", col(rows, "land"), logged=True, shift=1.0)
    numeric("beds", col(rows, "beds"))
    numeric("baths", col(rows, "baths"))
    numeric("cars", col(rows, "cars"))
    numeric("age", col(rows, "age"))

    # The council's own split of the CV. Where a council has valued the land but
    # not the buildings, land_value == cv and the CV is not a valuation of a
    # house at all — the ratio carries that, and it is exactly the case the
    # pricing pipeline already treats as an untrustworthy CV.
    lv_ratio = (col(rows, "land_value") / col(rows, "cv").replace(0, np.nan))
    numeric("land_value_share", lv_ratio.clip(0, 1.5))

    titles = rows["type_of_title"].map(title_bucket) if "type_of_title" in rows else \
        pd.Series("FH", index=rows.index)
    for cls in TITLE_CLASSES:
        cols[f"title_{cls}"] = (titles == cls).astype("float64")

    cols["months"] = t.astype("float64")

    names = sorted(cols)
    X = np.column_stack([cols[n].to_numpy(dtype="float64") for n in names])
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

    rows = rows.assign(_ctype=rows["property_type"].map(canonical_type)
                       if "property_type" in rows else "House")

    return Design(X=X, y=y, names=names, medians=med, rows=rows,
                  base_month=float(base_month), latest_month=float(_latest),
                  dropped={"unusable_rows": int((~keep).sum())})
