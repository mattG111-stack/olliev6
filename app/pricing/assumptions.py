"""Constants from the client's Excel 'Assumptions' tab.

Single source of truth for every tunable parameter the model uses.
"""

# === Comp-matching defaults (from Pricing Tool tab) ===
# A comparable must be the SAME product: identical bed and bath count, within
# 25% on both floor and land. A 3-bed is not a 4-bed with a tolerance.
COMP_BEDS_TOL = 0
COMP_BATHS_TOL = 0
COMP_FLOOR_PCT = 0.25
COMP_LAND_PCT = 0.25
COMP_MIN_PRICE = 50_000
# How far back a sale may be and still count as a comparable.
#
# PRICING USES THE CURRENT YEAR OF SALES ONLY. The deeper history that now loads
# alongside it is for trends — what a suburb has done over a decade — and must
# not reach the valuation engine: a 2012 sale is a fact about 2012, and mixing
# it into today's comps drags every estimate toward a market that no longer
# exists. Trend views read the full history directly and are unaffected by this.
#
# The bound used to be enforced by the FILE rather than by code: sold exports
# covered roughly the current year, so every row was recent and nothing had to
# check. Once files carry full history — the same house sold in 2026, 2020,
# 2012, 2002 and 1994 — an unfiltered median prices a $1.4M house at $335,000.
#
# Expressed as 12 months back from the most recent sale in the data rather than
# as the literal calendar year. Both mean "this year's sales" while a file is
# current, but a calendar-year test collapses every January — on 2 January the
# dataset holds two days of sales and every valuation loses its comps.
COMP_MAX_AGE_YEARS = 1
LIST_MULTIPLIER = 1.05

# === Rental yield tiers (from Assumptions tab) ===
YIELD_DEFAULT = 0.04
YIELD_PREMIUM_THRESHOLD = 2_000_000  # CV above -> 3% yield
YIELD_PREMIUM = 0.03
YIELD_AFFORDABLE_THRESHOLD = 800_000  # CV below -> 5.5% yield
YIELD_AFFORDABLE = 0.055

# === Operating expense ratios (% of gross rent) ===
OPEX_RATES = 0.08
OPEX_INSURANCE = 0.04
OPEX_MAINTENANCE = 0.05
OPEX_VACANCY = 0.04
OPEX_MANAGEMENT = 0.08
OPEX_TOTAL = OPEX_RATES + OPEX_INSURANCE + OPEX_MAINTENANCE + OPEX_VACANCY + OPEX_MANAGEMENT  # 0.29

# === Mortgage assumptions ===
# Superseded by CashflowAssumptions.deposit_pct (30% deposit = 70% LVR).
# Kept only so older scripts importing A.LVR keep working.
LVR = 0.65
MORTGAGE_RATE = 0.0675
LOAN_TERM_YEARS = 30

# === Subdivision ===
SUBDIVISION_YIELD_FACTOR = 0.75
DEMOLITION_COST = 30_000
SERVICES_COST_PER_LOT = 80_000
# Cap the practical lot count for any single property. Anything > 20 lots is a
# large-scale developer property that the residential-rate subdivision math
# doesn't price correctly (would need staged consents, infrastructure, etc).
MAX_PRACTICAL_LOTS_TOTAL = 20

# === Opportunity scoring weights (equal-weighted by default) ===
SCORE_WEIGHT_UNDERPRICED = 1.0
SCORE_WEIGHT_YIELD = 1.0
SCORE_WEIGHT_SUBDIVISION = 1.0

# === Section value fallback ($/m² when suburb not in bare-land table) ===
DEFAULT_SECTION_PRICE_PER_M2 = 1_000
SECTION_PRICE_MIN = 500

# Section $/m² used only when a suburb has neither bare-section sales nor
# council land values. A fallback, not a floor — a real suburb rate is used
# as-is even if it lands below this.
SECTION_RATE_FALLBACK = 850
SECTION_PRICE_MAX = 5_000

# === Property-type routing ===
# English + Chinese keywords. 建地 = vacant land, 乡村住宅建地 = rural residential land,
# 土地 = land, 地皮 = plot of land
VACANT_TYPE_KEYWORDS = ("vacant", "section", "bare land", "建地", "土地", "地皮")
COMMERCIAL_TYPE_KEYWORDS = ("commercial", "industrial", "other -", "商业", "工业")

# Estimate sanity gate — if our prediction is way off the asking price,
# the comps almost certainly weren't appropriate. Downgrade confidence.
ESTIMATE_VS_ASKING_MAX_RATIO = 3.0
ESTIMATE_VS_ASKING_MIN_RATIO = 0.33

# Asking price sanity at ingest — $1, $2 placeholder values from "by negotiation" listings
ASKING_PRICE_MIN = 10_000

# A council valuation this many times the asking price is not a bargain, it is a
# broken record: the CV attached to the wrong property, or a land-only figure, or
# a scrape that read the wrong number. $45M of CV on a $745k apartment.
#
# Written out separately in three modules until now — the pricing run, the
# hedonic and the audit — with no shared name between them. That is exactly how
# is_placeholder_price came to have three definitions that disagreed on 491
# listings: nothing was wrong with any one of them, and no two agreed.
CV_IMPLAUSIBLE_VS_ASKING = 2.5

# === Confidence tiers (based on comp count) ===
CONFIDENCE_HIGH_MIN = 10
CONFIDENCE_MEDIUM_MIN = 3
CONFIDENCE_LOW_MIN = 1


def expected_yield_for_cv(cv: float | None) -> float:
    """Yield tier based on Council Valuation."""
    if cv is None:
        return YIELD_DEFAULT
    if cv > YIELD_PREMIUM_THRESHOLD:
        return YIELD_PREMIUM
    if cv < YIELD_AFFORDABLE_THRESHOLD:
        return YIELD_AFFORDABLE
    return YIELD_DEFAULT


def confidence_tier(comps_count: int) -> str:
    if comps_count >= CONFIDENCE_HIGH_MIN:
        return "high"
    if comps_count >= CONFIDENCE_MEDIUM_MIN:
        return "medium"
    if comps_count >= CONFIDENCE_LOW_MIN:
        return "low"
    return "insufficient"


def is_vacant_type(property_type: str | None) -> bool:
    if not property_type:
        return False
    s = str(property_type).lower()
    return any(k in s for k in VACANT_TYPE_KEYWORDS)


def is_commercial_type(property_type: str | None) -> bool:
    if not property_type:
        return False
    s = str(property_type).lower()
    return any(k in s for k in COMMERCIAL_TYPE_KEYWORDS)


def parse_money(v) -> float | None:
    """A scraped money string to a number: "$1.35M" / "$599K" / "$1,850,000".

    The feed writes most figures twice — a display string and a _numeric twin —
    and the load reads the twin. valuation_last_sold_value is the exception: it
    has no twin, it arrives as "$1.35M", and it was being read with the plain
    float() path, which returns nothing for it.

    Nothing raised. The date beside it is stored as text and survived, so 5,104
    of 9,102 listings ended up knowing WHEN a house last sold and not for how
    much. It lives here because both the load and the pricing run need it, and
    the pricing run had its own copy.
    """
    if v is None:
        return None
    t = str(v).replace("$", "").replace(",", "").strip().upper()
    if not t or t in ("NAN", "NONE", "NAT"):
        return None
    mult = 1.0
    if t.endswith("K"):
        mult, t = 1_000.0, t[:-1]
    elif t.endswith("M"):
        mult, t = 1_000_000.0, t[:-1]
    try:
        f = float(t) * mult
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


# How far a VALUATION may sit above an asking price and still be a find.
#
# Two numbers said this, in two places, and they disagreed. The hedonic refused
# anything over 1.6x with the note "genuine below-value deals top out around
# +50%, so 1.6x keeps real deals while cutting the false ones". The deal guard
# in the pricing run said 1.8x. Nothing sat between them on purpose — it was
# simply never noticed that they were the same rule written twice.
#
# 42A Woodlands Crescent lived in that gap: $999,000 asking, valued $1,792,500,
# 1.794x, published as the biggest gap on the market at a 79.6% margin. A 3-bed
# 1-bath does not carry a 79.6% discount; an input is wrong, and the guard that
# says so was set four thousandths too high to fire.
#
# 1.6 is the considered figure. The 1.8 was the outlier.
MAX_VALUE_VS_ASKING = 1.6

# And the other side of the same band: a valuation far BELOW the asking is as
# broken as one far above, and for the same reason.
MIN_VALUE_VS_ASKING = 0.5
