"""What a pool is worth HERE, measured rather than assumed.

The model carried one number for a pool — POOL_PREMIUM, 2.9%, the same in every
suburb — and the comp engine ignored pools entirely. So a house with a pool was
calibrated against sales of houses without one, and a house without a pool was
calibrated against sales of houses with one, and neither was told.

Two things fix that, in this order:

  1. Compare like with like. If there are enough sales in the area that match
     the subject's pool status, those are the comparables. No premium is
     assumed, estimated, or applied — the question never comes up.

  2. When there are not enough, measure the gap. Inside a suburb, property type,
     bed count and floor-area band, compare what homes WITH a pool sold for
     against what homes WITHOUT one sold for. That is the difference between a
     four-bedroom with a pool and a four-bedroom without, in that area, which is
     the only honest way to put the two on the same footing.

Held constant, deliberately: bedrooms and floor area. Without them the
comparison measures size — pools sit on bigger sections, in bigger houses, in
better streets. Auckland-wide the raw gap is +19.9%; a pool does not add a fifth
to the value of a house. Even size-controlled the number is an ASSOCIATION and
not a cause, which is why it is capped (POOL_MAX) rather than trusted at face
value. The cap binds in a handful of areas and its job is to stop one $4M sale
with a pool from re-pricing a suburb.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median

import pandas as pd

from .glm import POOL_PREMIUM, canonical_type
from .valueadd import _cells

# The flat, nationwide figure the hedonic has always used. Still the answer when
# an area has nothing measurable to say.
DEFAULT_PREMIUM = POOL_PREMIUM - 1.0

# A measured gap outside this range is not a pool. Below zero says the pool
# houses in that cell happened to be worse houses; far above says the comparison
# is still measuring something other than the pool.
POOL_MIN, POOL_MAX = 0.0, 0.10

# Cells needed before a scope's own measurement beats the wider one. `_cells`
# already requires MIN_PER_SIDE sales on each side of every cell it returns.
MIN_CELLS_SUBURB = 4
MIN_CELLS = 3


@dataclass(frozen=True)
class PoolFactor:
    """The premium used for one listing, and where it came from."""
    pct: float                 # 0.029 = 2.9%
    scope: str                 # "suburb" | "district" | "type" | "default"
    cells: int                 # how many size x bed cells stood behind it
    capped: bool = False       # the measured figure hit POOL_MAX

    @property
    def factor(self) -> float:
        return 1.0 + self.pct


# ---- Does this property already have one? -----------------------------------
# Read once at ingest, so nothing downstream has to ask twice or ask differently.
#
# The export's own flag is the first answer, but it is often simply not filled
# in — and a listing that spends a paragraph on the swimming pool has told us
# anyway. Getting this wrong in the FALSE direction is not a rounding error: the
# property page offers "houses here with a pool sell for +X%" to a house that
# already has one, which is nonsense on the screen of the person who owns it.
#
# Phrases stripped before the search, because in a listing they do not mean a
# swimming pool. "Spa pool" is the important one — in New Zealand that is a hot
# tub, and it appears in thousands of listings.
_POOL_NOT = re.compile(
    r"\b("
    r"spa[\s-]?pool|pool[\s-]?table|pool[\s-]?room|car[\s-]?pool|carpool|"
    r"pools? of (?:light|natural light|sunlight)|gene pool|talent pool|"
    r"(?:school|public|community|council|local|olympic|town)[\s-]?pool|"
    r"pool[\s-]?complex|swim(?:ming)?[\s-]?school"
    r")\b", re.I)

# A pool nearby is not a pool. These are the ways a listing says "not here".
_POOL_ELSEWHERE = re.compile(
    r"\b(?:near(?:by|est)?|close to|walk(?:ing)? (?:distance )?to|handy to|"
    r"minutes? (?:from|to)|access to|shared|communal|body corporate)\b"
    r"[^.]{0,40}\bpool", re.I)

_POOL_NO = re.compile(r"\b(?:no|without|lacks?|not?\s+a)\s+(?:\w+\s+){0,2}pool", re.I)
_POOL_YES = re.compile(r"\bpools?\b", re.I)

# Fields worth reading. Long free text first — that is where a pool gets sold.
_POOL_FIELDS = ("description", "estate_description", "key_facts", "other_features",
                "listing_title", "name")


def text_says_pool(*texts) -> bool:
    """True when the words describe a pool ON this property.

    Deliberately conservative. A false positive silently switches a house onto a
    different set of comparable sales, so anything ambiguous is left alone.
    """
    for raw in texts:
        if not raw:
            continue
        s = str(raw)
        if not s.strip():
            continue
        s = _POOL_NOT.sub(" ", s)
        if not _POOL_YES.search(s):
            continue
        if _POOL_NO.search(s) or _POOL_ELSEWHERE.search(s):
            continue
        return True
    return False


def _truthy(v) -> bool:
    return v in (True, 1, "1", "True", "true", "TRUE", "Yes", "yes", "Y", "y")


def _field(row, key):
    """One field, whether `row` is an import dict or a stored property row."""
    if hasattr(row, "get"):
        try:
            return row.get(key)
        except (TypeError, KeyError):
            pass
    return getattr(row, key, None)


def detect_pool(row) -> bool:
    """Whether a property has a pool, from every source that might say so.

    In order: the export's own flag, its keyword flag, then the listing text.
    Any one of them is enough — a blank flag means "not recorded", not "no".

    Takes either a row being imported or one already stored, because it is
    needed in both places: at ingest so the whole system agrees, and at read
    time so properties loaded before this existed are not still being offered a
    pool they already have.
    """
    if _truthy(_field(row, "has_swimming_pool")) or _truthy(_field(row, "kw_pool")):
        return True
    return text_says_pool(*(_field(row, f) for f in _POOL_FIELDS))


def _frame(sold: pd.DataFrame) -> pd.DataFrame:
    """The columns `_cells` needs, whatever the caller's frame is called."""
    if sold is None or len(sold) == 0:
        return pd.DataFrame(columns=["suburb", "district", "property_type",
                                     "beds", "floor", "price", "pool"])
    out = pd.DataFrame({
        "suburb": sold.get("suburb"),
        "district": sold.get("district"),
        "property_type": sold.get("ct") if "ct" in sold.columns
        else sold.get("property_type", pd.Series(dtype=object)).map(canonical_type),
        "beds": pd.to_numeric(sold.get("beds"), errors="coerce"),
        "floor": pd.to_numeric(
            sold.get("floor_area_m2", sold.get("floor")), errors="coerce"),
        "price": pd.to_numeric(
            sold.get("sale_price", sold.get("price")), errors="coerce"),
        "pool": pool_flags(sold),
    })
    return out.dropna(subset=["price", "floor"])


def pool_flags(df: pd.DataFrame) -> pd.Series:
    """The pool column as real booleans.

    It arrives as True/False from the database, as the strings "True"/"true"
    from a CSV, as 1/0 from a spreadsheet, and as NaN when the export did not
    say. Unknown means no: claiming a pool we were never told about would move
    a valuation on nothing.
    """
    if df is None or len(df) == 0:
        return pd.Series(dtype=bool)
    col = df.get("has_swimming_pool")
    if col is None:
        col = df.get("pool")
    if col is None:
        return pd.Series(False, index=df.index)
    return col.map(lambda v: v in (True, 1, "1", "True", "true", "TRUE", "Yes", "yes")).astype(bool)


class PoolPremium:
    """Pool premiums by area, built once from a sold frame and reused per row."""

    def __init__(self, sold: pd.DataFrame):
        self._df = _frame(sold)
        self._cache: dict[tuple, PoolFactor] = {}

    def _measure(self, subset: pd.DataFrame) -> tuple[float | None, int]:
        if subset is None or subset.empty:
            return None, 0
        cells = _cells(subset, "pool", 0, 1, pool=True, hold="beds")
        return (median(cells) if cells else None), len(cells)

    def premium(self, *, suburb: str | None, district: str | None,
                property_type: str | None = None) -> PoolFactor:
        """Suburb first, then district, then this property type across the
        region — the first scope with enough cells to mean anything."""
        ctype = canonical_type(property_type) if property_type else None
        key = (suburb, district, ctype)
        if key in self._cache:
            return self._cache[key]

        df = self._df
        if ctype and "property_type" in df.columns:
            typed = df[df.property_type == ctype]
            if len(typed) >= 20:      # only narrow when it leaves a usable sample
                df = typed

        for scope, subset, need in (
            ("suburb", df[df.suburb == suburb] if suburb else df.iloc[0:0], MIN_CELLS_SUBURB),
            ("district", df[df.district == district] if district else df.iloc[0:0], MIN_CELLS),
            ("type", df, MIN_CELLS),
        ):
            pct, n = self._measure(subset)
            if pct is not None and n >= need:
                capped = pct > POOL_MAX
                pct = min(max(pct, POOL_MIN), POOL_MAX)
                out = PoolFactor(pct=round(pct, 4), scope=scope, cells=n, capped=capped)
                self._cache[key] = out
                return out

        out = PoolFactor(pct=DEFAULT_PREMIUM, scope="default", cells=0)
        self._cache[key] = out
        return out


def to_pool_status(price: float, *, comp_pool: bool, subject_pool: bool,
                   pct: float) -> float:
    """A comp's sale price, restated as if it had the subject's pool status.

    Only used when there were not enough like-for-like sales to compare against.
    Same status either way — which is the common case — returns the price
    untouched.
    """
    if comp_pool == subject_pool:
        return float(price)
    if subject_pool:                 # comp has none, subject does: add the gap
        return float(price) * (1.0 + pct)
    return float(price) / (1.0 + pct)  # comp has one, subject does not
