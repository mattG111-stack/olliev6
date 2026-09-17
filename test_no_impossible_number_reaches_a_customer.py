"""Nothing indefensible on a page, on any row, ever.

    "now run more tests no more bugs i need to be live in a week"

Every bug in this codebase that mattered was the same shape: a number reached a
customer that we could not defend if asked where it came from. A +501% price
rise. A 79.6% margin from a council record that valued the land and not the
house. A "buy price" above the asking price. A subdivision on a cross-lease.

Individual guards for each of those exist and are tested. This is the other
direction: run a large, varied batch through the REAL pipeline and assert that
no row comes out the far end carrying a figure that cannot be true — whatever
combination of inputs produced it.

It is deliberately about ARITHMETIC AND MEANING rather than about accuracy. It
does not claim a valuation is right; it claims a valuation is possible. Those
are different tests and only one of them can be written without a crystal ball.

The batch includes hostile rows on purpose — no floor area, no CV, zero land, a
negative area, an absurd asking — because a launch week is exactly when the
worst row in the file gets published.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.pricing.comps import SoldDataset
from app.pricing.pipeline import run as run_pipeline

# Every numeric column the app can put in front of somebody.
MONEY = ["fair_value", "market_value", "buy_price", "range_low", "range_high",
         "gross_sales", "subdivision_profit", "best_net_gain",
         "total_subdivided_value", "services_cost", "annual_gross_rent",
         "annual_net_rent", "annual_cashflow", "est_weekly_rent"]
RATIOS = ["margin", "est_gross_yield", "cash_on_cash", "breakeven_deposit_pct",
          "uplift_vs_asking"]


def _sold(n=1500, seed=7):
    rng = np.random.default_rng(seed)
    subs = ["Riverhead", "Glenfield", "Papakura", "Mount Albert"]
    eff = {"Riverhead": 0.18, "Glenfield": 0.0, "Papakura": -0.22, "Mount Albert": 0.10}
    s = rng.choice(subs, n)
    cv = rng.lognormal(14.0, 0.35, n)
    floor = rng.normal(190, 45, n).clip(60, 500)
    beds = rng.integers(2, 6, n)
    ln = (np.log(cv) + 0.04 + 0.10 * np.log(floor / 190.0) + 0.05 * (beds - 4)
          + np.array([eff[x] for x in s]) + rng.normal(0, 0.06, n))
    return pd.DataFrame({
        "suburb": s,
        "district": ["Rodney" if x == "Riverhead" else "Auckland City" for x in s],
        "property_type": "House", "type_of_title": "1.0",
        "address": [f"{i} Sold Street" for i in range(n)],
        "price_numeric": np.exp(ln), "cv_numeric": cv,
        "key_floor_area": floor,
        "key_land_area": rng.normal(700, 150, n).clip(200, 2000),
        "key_bedrooms": beds, "key_bathrooms": rng.integers(1, 4, n),
        "land_value_numeric": cv * 0.55, "building_age": 2005,
        "sold_date": pd.Timestamp("2026-06-01"),
    })


def _live(n=600, seed=13):
    """A realistic batch, plus the rows that break things."""
    rng = np.random.default_rng(seed)
    subs = ["Riverhead", "Glenfield", "Papakura", "Mount Albert"]
    cv = rng.lognormal(14.0, 0.35, n)
    floor = rng.normal(190, 45, n).clip(60, 500)
    df = pd.DataFrame({
        "suburb": rng.choice(subs, n),
        "district": "Auckland City",
        "property_type": rng.choice(["House", "Townhouse", "Section", "Apartment"], n),
        "type_of_title": rng.choice(["Freehold", "Fee Simple", "Cross-Lease",
                                     "Unit Title", "1.0", None], n),
        "address": [f"{i} Live Road" for i in range(n)],
        "price_numeric": cv * rng.normal(0.98, 0.12, n),
        "price_display": rng.choice(["Asking price", "Auction", "By negotiation"], n),
        "cv_numeric": cv,
        "key_floor_area": floor,
        "key_land_area": rng.normal(750, 300, n).clip(0, 3000),
        "key_bedrooms": rng.integers(1, 6, n),
        "key_bathrooms": rng.integers(1, 4, n),
        "key_carspaces": rng.integers(0, 3, n),
        "land_value_numeric": cv * 0.55,
        "improvement_value_numeric": cv * 0.45,
        "building_age": 2005,
        "has_swimming_pool": False,
        "zoning": rng.choice([
            "Residential - Mixed Housing Suburban Zone",
            "Residential - Single House Zone",
            "Residential - Terrace Housing and Apartment Building Zone",
            None], n),
    })

    # The rows that break things. A launch week is exactly when the worst row
    # in the file gets published, so they go through the same pipeline.
    hostile = pd.DataFrame([
        # no floor area at all
        dict(suburb="Papakura", district="Auckland City", property_type="House",
             type_of_title="Freehold", address="1 No Floor Way",
             price_numeric=900_000.0, price_display="Asking price",
             cv_numeric=950_000.0, key_floor_area=None, key_land_area=800.0,
             key_bedrooms=3, key_bathrooms=1, key_carspaces=1,
             land_value_numeric=500_000.0, improvement_value_numeric=450_000.0,
             building_age=1975, has_swimming_pool=False,
             zoning="Residential - Mixed Housing Suburban Zone"),
        # no council record
        dict(suburb="Glenfield", district="Auckland City", property_type="House",
             type_of_title="Freehold", address="2 No Council Record Road",
             price_numeric=1_200_000.0, price_display="Asking price",
             cv_numeric=None, key_floor_area=150.0, key_land_area=600.0,
             key_bedrooms=3, key_bathrooms=2, key_carspaces=1,
             land_value_numeric=None, improvement_value_numeric=None,
             building_age=2001, has_swimming_pool=False, zoning=None),
        # zero land
        dict(suburb="Riverhead", district="Rodney", property_type="Apartment",
             type_of_title="Unit Title", address="3 Zero Land Lane",
             price_numeric=650_000.0, price_display="Asking price",
             cv_numeric=700_000.0, key_floor_area=70.0, key_land_area=0.0,
             key_bedrooms=2, key_bathrooms=1, key_carspaces=0,
             land_value_numeric=0.0, improvement_value_numeric=700_000.0,
             building_age=2015, has_swimming_pool=False, zoning=None),
        # NEGATIVE land area — a real export carried these
        dict(suburb="Papakura", district="Auckland City", property_type="House",
             type_of_title="Freehold", address="4 Negative Land Drive",
             price_numeric=800_000.0, price_display="Asking price",
             cv_numeric=850_000.0, key_floor_area=140.0, key_land_area=-4.0,
             key_bedrooms=3, key_bathrooms=1, key_carspaces=1,
             land_value_numeric=400_000.0, improvement_value_numeric=450_000.0,
             building_age=1990, has_swimming_pool=False,
             zoning="Residential - Mixed Housing Suburban Zone"),
        # an absurd asking price
        dict(suburb="Mount Albert", district="Auckland City", property_type="House",
             type_of_title="Freehold", address="5 Absurd Price Place",
             price_numeric=99_000_000.0, price_display="Asking price",
             cv_numeric=1_100_000.0, key_floor_area=160.0, key_land_area=700.0,
             key_bedrooms=4, key_bathrooms=2, key_carspaces=2,
             land_value_numeric=600_000.0, improvement_value_numeric=500_000.0,
             building_age=1998, has_swimming_pool=False,
             zoning="Residential - Mixed Housing Suburban Zone"),
        # no price at all
        dict(suburb="Glenfield", district="Auckland City", property_type="House",
             type_of_title="Freehold", address="6 No Price Rise",
             price_numeric=None, price_display="By negotiation",
             cv_numeric=1_000_000.0, key_floor_area=155.0, key_land_area=650.0,
             key_bedrooms=3, key_bathrooms=2, key_carspaces=1,
             land_value_numeric=550_000.0, improvement_value_numeric=450_000.0,
             building_age=2003, has_swimming_pool=False, zoning=None),
    ])
    return pd.concat([df, hostile], ignore_index=True)


@pytest.fixture(scope="module")
def priced():
    """One real pipeline run over 606 listings, shared by every assertion."""
    out = run_pipeline(_live(), SoldDataset(_sold()))
    assert len(out) == 606, "the pipeline dropped rows"
    return out.reset_index(drop=True)


def _num(out, col):
    if col not in out.columns:
        pytest.skip(f"{col} is not produced by this pipeline")
    return pd.to_numeric(out[col], errors="coerce")


# ---- nothing that is not a number ------------------------------------------
@pytest.mark.parametrize("col", MONEY + RATIOS)
def test_no_infinity_reaches_a_row(priced, col):
    """NaN is survivable — it renders as an em dash and says "we don't know".
    Infinity is not: it serialises to JSON as the bare token Infinity, which is
    not valid JSON, and the page that receives it shows nothing at all."""
    v = _num(priced, col)
    assert not np.isinf(v.dropna()).any(), \
        f"{col} carries an infinity on {int(np.isinf(v.dropna()).sum())} row(s)"


@pytest.mark.parametrize("col", MONEY)
def test_money_is_never_negative_where_it_cannot_be(priced, col):
    """Profit and cashflow can legitimately be negative — a deal that loses
    money is a real answer. A VALUE cannot be."""
    if col in ("subdivision_profit", "best_net_gain", "annual_cashflow"):
        pytest.skip("a loss is a real answer for this one")
    v = _num(priced, col).dropna()
    assert (v >= 0).all(), f"{col} went negative on {int((v < 0).sum())} row(s)"


# ---- the numbers that face a buyer ------------------------------------------
def test_the_buy_price_never_exceeds_the_asking_price(priced):
    """"What to pay" above "what they want" is not a recommendation, it is a
    typo with a dollar sign. It has happened here before."""
    if "buy_price" not in priced.columns:
        pytest.skip("no buy_price column")
    ask = _num(priced, "price_numeric")
    buy = _num(priced, "buy_price")
    both = ask.notna() & buy.notna() & (ask > 0)
    over = both & (buy > ask * 1.0001)
    assert not over.any(), (
        f"{int(over.sum())} row(s) tell a buyer to pay more than the vendor asked, "
        f"e.g. {priced.loc[over, 'address'].head(3).tolist()}")


def test_the_margin_matches_the_two_numbers_it_is_drawn_from(priced):
    """A margin is not an independent figure — it is (value − asking) / asking.
    If it disagrees with the two numbers printed beside it, one of the three is
    wrong and the page contradicts itself."""
    ask = _num(priced, "price_numeric")
    fv = _num(priced, "fair_value")
    margin = _num(priced, "margin")
    ok = ask.notna() & fv.notna() & margin.notna() & (ask > 0)
    implied = (fv[ok] - ask[ok]) / ask[ok]
    drift = (implied - margin[ok]).abs()
    assert (drift < 0.01).all(), (
        f"the margin disagrees with value-minus-asking on "
        f"{int((drift >= 0.01).sum())} row(s); worst is {drift.max():.3f}")


def test_no_margin_above_the_believable_ceiling(priced):
    """Every one of the +206%, +2,296% headlines was a council record that
    valued the land and not the house."""
    from app.routers.properties import MARGIN_MAX_PCT

    m = _num(priced, "margin").dropna()
    over = m[m > MARGIN_MAX_PCT]
    assert over.empty, f"{len(over)} row(s) above the ceiling, worst {over.max():.1%}"


def test_the_score_is_a_percentage(priced):
    s = _num(priced, "opportunity_score_pct").dropna()
    assert ((s >= 0) & (s <= 100)).all(), \
        f"opportunity score outside 0–100: min {s.min()}, max {s.max()}"


def test_the_valuation_range_contains_the_valuation(priced):
    """THE BUG THIS SWEEP FOUND. The band is computed around the AVM's own
    market_value; the page shows fair_value, which is CV-anchored and can differ
    by a long way. On 198 of 473 rows a customer would have read

        Our valuation $3,181,962 ... likely range $728,000-$964,000

    which is not a range with a mistake in it — it is a range for a different
    number entirely."""
    lo, fv, hi = (_num(priced, "range_low"), _num(priced, "fair_value"),
                  _num(priced, "range_high"))
    ok = lo.notna() & hi.notna() & fv.notna()
    bad = ok & ((fv < lo * 0.999) | (fv > hi * 1.001))
    assert not bad.any(), (
        f"{int(bad.sum())} row(s) show a valuation outside the range printed "
        f"beside it")


def test_the_band_is_ordered(priced):
    lo, hi = _num(priced, "range_low"), _num(priced, "range_high")
    ok = lo.notna() & hi.notna()
    assert (lo[ok] <= hi[ok]).all(), "a band's low end is above its high end"


def test_the_band_keeps_the_width_it_was_measured_with(priced):
    """Moving the band onto the published figure must not quietly widen or
    narrow it — that would be inventing confidence rather than reporting it.
    The spec is ±4% on the asking path and ±14% on the fallback, so nothing
    should come out beyond a whisker of 8% or 28% of its own value."""
    lo, hi, fv = _num(priced, "range_low"), _num(priced, "range_high"), _num(priced, "fair_value")
    ok = lo.notna() & hi.notna() & fv.notna() & (fv > 0)
    width = (hi[ok] - lo[ok]) / fv[ok]
    assert (width <= 0.30).all(), f"a band widened to {width.max():.1%} of the value"
    assert (width >= 0.05).all(), f"a band narrowed to {width.min():.1%} of the value"


@pytest.mark.parametrize("edge,around,shown,expected", [
    (95_000.0, 100_000.0, 200_000.0, 190_000.0),   # scales with the target
    (95_000.0, 100_000.0, 100_000.0, 95_000.0),    # unchanged when they agree
    (None, 100_000.0, 200_000.0, None),            # no band to move
    (95_000.0, None, 200_000.0, None),             # nothing to measure against
    (95_000.0, 100_000.0, None, None),             # nothing published
    (95_000.0, 0.0, 200_000.0, None),              # would divide by zero
    (95_000.0, float("nan"), 200_000.0, None),
    (float("inf"), 100_000.0, 200_000.0, None),
    (95_000.0, 100_000.0, float("inf"), None),
])
def test_a_band_that_cannot_be_moved_honestly_is_not_moved(edge, around, shown, expected):
    """None rather than a guess. A band that has silently become "the value ±
    nothing" reads as certainty, which is the opposite of what it is for."""
    from app.pricing.pipeline import _rescale_band

    assert _rescale_band(edge, around, shown) == expected


def test_a_yield_is_a_plausible_yield(priced):
    """A gross yield outside 0–30% is arithmetic, not a property."""
    y = _num(priced, "est_gross_yield").dropna()
    assert ((y >= 0) & (y <= 0.30)).all(), \
        f"gross yield outside 0–30%: min {y.min():.3f}, max {y.max():.3f}"


# ---- the subdivision claims -------------------------------------------------
def test_a_subdividable_site_actually_splits(priced):
    """"Subdividable" with fewer than two sections is a contradiction, and it is
    the claim that sends somebody to look at a property."""
    if "is_subdividable" not in priced.columns:
        pytest.skip("no subdivision output")
    flag = priced["is_subdividable"].fillna(False).astype(bool)
    sections = _num(priced, "sections")
    bad = flag & ~(sections >= 2)
    assert not bad.any(), \
        f"{int(bad.sum())} row(s) flagged subdividable with under two sections"


def test_a_subdividable_site_makes_money(priced):
    """A site that loses money is not an opportunity. The figures are still
    computed so the page can show why the answer is no — but the FLAG must not
    be set."""
    if "is_subdividable" not in priced.columns:
        pytest.skip("no subdivision output")
    flag = priced["is_subdividable"].fillna(False).astype(bool)
    gain = _num(priced, "best_net_gain")
    bad = flag & gain.notna() & (gain <= 0)
    assert not bad.any(), \
        f"{int(bad.sum())} site(s) flagged as an opportunity while losing money"


def test_a_title_that_cannot_be_divided_is_never_flagged(priced):
    """A cross-lease or unit-title owner cannot divide the land, whatever the
    arithmetic says about the site."""
    if "is_subdividable" not in priced.columns:
        pytest.skip("no subdivision output")
    from app.pricing.buyprice import _title_bucket

    flag = priced["is_subdividable"].fillna(False).astype(bool)
    buckets = priced["type_of_title"].map(lambda t: _title_bucket(t) if t is not None else "OT")
    bad = flag & (buckets != "FH")
    assert not bad.any(), (
        f"{int(bad.sum())} non-freehold row(s) flagged subdividable: "
        f"{priced.loc[bad, 'type_of_title'].unique().tolist()}")


# ---- the underpriced claim --------------------------------------------------
def test_underpriced_means_the_value_beats_the_asking(priced):
    """The one claim the whole product rests on."""
    if "is_underpriced" not in priced.columns:
        pytest.skip("no underpriced flag")
    flag = priced["is_underpriced"].fillna(False).astype(bool)
    ask, fv = _num(priced, "price_numeric"), _num(priced, "fair_value")
    bad = flag & ask.notna() & fv.notna() & (fv <= ask)
    assert not bad.any(), \
        f"{int(bad.sum())} row(s) called underpriced while valued at or below the asking"


def test_the_hostile_rows_did_not_take_the_batch_down(priced):
    """The whole point of putting them in: one poisoned row must cost its own
    listing, never the load. A negative land area used to raise inside a log and
    end the run for every row behind it."""
    addresses = set(priced["address"])
    for a in ["1 No Floor Way", "2 No Council Record Road", "3 Zero Land Lane",
              "4 Negative Land Drive", "5 Absurd Price Place", "6 No Price Rise"]:
        assert a in addresses, f"{a} disappeared from the batch"
