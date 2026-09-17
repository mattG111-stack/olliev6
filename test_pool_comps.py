"""A pool changes which sales a house can be compared against.

Until now it did not. The comp engine matched on suburb, type, bedrooms,
bathrooms and land, and never looked at whether a sale had a pool — so a house
with one was calibrated against sales of houses without, a house without one was
calibrated against sales of houses with, and the valuation carried the
difference silently. The only pool figure anywhere was the hedonic's flat 2.9%,
the same number in every suburb in the country.

Two rules now, in this order:

  1. Compare like with like. Enough sales matching the subject's pool status and
     those are the comparables — no premium is assumed anywhere.
  2. Otherwise measure the gap in that area, between homes with a pool and homes
     without, holding bedrooms and floor area, and restate the comps onto the
     subject's footing.

The second is the user's own description: "if there isn't enough houses with
pools to do the comparison then we just work out what the difference is between
a four bedroom house without a pool versus a four bedroom house with a pool,
2.5% or whatever it is in that area."
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.pricing.buyprice import CompEngine
from app.pricing.glm import POOL_PREMIUM
from app.pricing.pool import (
    DEFAULT_PREMIUM,
    POOL_MAX,
    PoolPremium,
    pool_flags,
    to_pool_status,
)

# Enough shape for the cell machinery to have something to measure: three bed
# counts across four size bands is twelve cells, each with sales on both sides.
BEDS = (3, 4, 5)
FLOORS = (120, 160, 200, 240)


def _base_price(beds: int, floor: int) -> float:
    return 700_000 + beds * 120_000 + floor * 1_500


def _market(*, suburb="Mount Eden", district="Auckland City", pool_gap=0.08,
            per_cell=4, pools=True, start=0):
    """A suburb of sales where a pool sells for exactly `pool_gap` more.

    The council valuation is the same either way — it is the SALE that carries
    the pool — so the sale/value ratio the engine calibrates on genuinely
    differs between the two, which is the thing being measured.
    """
    rows, n = [], start
    for beds in BEDS:
        for floor in FLOORS:
            cv = _base_price(beds, floor)
            for has_pool in ((False, True) if pools else (False,)):
                for i in range(per_cell):
                    n += 1
                    rows.append({
                        "slug_id": f"p{n}", "address": f"{n} Pool Road",
                        "suburb": suburb, "district": district,
                        "property_type": "House", "beds": beds, "baths": 2, "cars": 2,
                        "floor_area_m2": floor, "land_area_m2": 600,
                        "sale_price": cv * (1 + pool_gap if has_pool else 1) + i * 500,
                        "cv_numeric": cv, "sold_date": "2026-05-15",
                        "type_of_title": "Freehold", "has_swimming_pool": has_pool,
                        "building_age": 1990,
                    })
    return rows


def _frame(*blocks):
    return pd.DataFrame([r for b in blocks for r in b])


SUBJECT = dict(suburb="Mount Eden", district="Auckland City", property_type="House",
               beds=4, baths=2, land=600, asking=None, cv=_base_price(4, 200))


# ---- the flag itself --------------------------------------------------------
def test_every_spelling_of_a_pool_reads_as_a_pool():
    """True from the database, "true" from a CSV, 1 from a spreadsheet."""
    df = pd.DataFrame({"has_swimming_pool": [True, "True", "true", 1, "1", "Yes"]})
    assert pool_flags(df).all()


def test_no_pool_and_not_saying_both_mean_no_pool():
    """Claiming a pool we were never told about would move a valuation on nothing."""
    df = pd.DataFrame({"has_swimming_pool": [False, None, "", "no", float("nan")]})
    assert not pool_flags(df).any()


# ---- measuring the gap ------------------------------------------------------
def test_the_premium_is_measured_from_this_areas_own_sales():
    """Same suburb, same beds, same size band — the pool is the only difference."""
    pf = PoolPremium(_frame(_market(pool_gap=0.05))).premium(
        suburb="Mount Eden", district="Auckland City", property_type="House")
    assert pf.scope == "suburb", f"fell through to {pf.scope} on a suburb full of sales"
    assert pf.pct == pytest.approx(0.05, abs=0.01), f"measured {pf.pct:.1%} on a 5% gap"


def test_a_different_area_gets_its_own_number():
    """The whole point of measuring rather than assuming."""
    df = _frame(_market(pool_gap=0.02),
                _market(suburb="Remuera", district="Auckland City",
                        pool_gap=0.09, start=5000))
    pp = PoolPremium(df)
    quiet = pp.premium(suburb="Mount Eden", district="Auckland City",
                       property_type="House")
    rich = pp.premium(suburb="Remuera", district="Auckland City",
                      property_type="House")
    assert quiet.pct < rich.pct, f"{quiet.pct:.1%} vs {rich.pct:.1%} — one number for both"


def test_a_suburb_with_nothing_to_say_falls_through_to_the_default():
    """Two sales and no pools is not a measurement."""
    pf = PoolPremium(pd.DataFrame(_market(per_cell=1, pools=False))).premium(
        suburb="Nowhere", district="Nowhere", property_type="House")
    assert pf.scope == "default"
    assert pf.pct == pytest.approx(DEFAULT_PREMIUM)


def test_an_implausible_gap_is_capped_rather_than_believed():
    """A pool does not add a third to the value of a house.

    Auckland-wide the raw gap reads +19.9%, because pools sit in bigger homes on
    better streets. Size and bedrooms are held to strip most of that out, and
    what survives is still association rather than cause — so it is capped.
    """
    pf = PoolPremium(_frame(_market(pool_gap=0.40))).premium(
        suburb="Mount Eden", district="Auckland City", property_type="House")
    assert pf.pct == POOL_MAX and pf.capped


def test_a_gap_the_wrong_way_round_is_not_a_negative_pool():
    pf = PoolPremium(_frame(_market(pool_gap=-0.15))).premium(
        suburb="Mount Eden", district="Auckland City", property_type="House")
    assert pf.pct >= 0.0


def test_restating_a_price_onto_the_subjects_footing():
    # Comp has no pool, subject does → the comp is worth more with one.
    assert to_pool_status(1_000_000, comp_pool=False, subject_pool=True,
                          pct=0.05) == pytest.approx(1_050_000)
    # Comp has one, subject does not → strip it back out.
    assert to_pool_status(1_050_000, comp_pool=True, subject_pool=False,
                          pct=0.05) == pytest.approx(1_000_000)
    # Same either way → untouched, which is the ordinary case.
    for same in (True, False):
        assert to_pool_status(999_999, comp_pool=same, subject_pool=same,
                              pct=0.05) == 999_999


# ---- the engine -------------------------------------------------------------
def _valued(eng: CompEngine, *, pool: bool, v4: float):
    """What the engine says this house is worth, priced as the pipeline does it.

    The hedonic applies its flat premium before the engine ever sees the
    listing, so a pool house arrives with a larger v4 than the same house
    without one. Both are passed here exactly as they would be in production.
    """
    return eng.buy_price(**SUBJECT, pool=pool,
                         v4_value=v4 * (POOL_PREMIUM if pool else 1.0))


def test_like_for_like_comps_are_used_when_there_are_enough():
    """No premium is assumed, estimated or applied — the question never arises."""
    eng = CompEngine(_frame(_market()))
    res = eng.buy_price(**SUBJECT, pool=True, v4_value=1_400_000)
    assert res.pool_basis == "matched", (
        f"{res.pool_basis}: a suburb full of pool sales still went to an estimate"
    )
    assert res.pool_pct is None


def test_the_areas_own_gap_decides_the_difference_not_a_flat_national_number():
    """The headline. A pool is worth what pools are worth HERE.

    Two identical subjects in a suburb where a pool sells for 8% more. The gap
    between their values has to be that 8%, not the model's flat 2.9%.
    """
    eng = CompEngine(_frame(_market(pool_gap=0.08)))
    with_pool = _valued(eng, pool=True, v4=1_400_000)
    without = _valued(eng, pool=False, v4=1_400_000)
    gap = with_pool.area_value / without.area_value - 1
    assert gap == pytest.approx(0.08, abs=0.015), (
        f"the pool was worth {gap:.1%} where the suburb's own sales say 8%"
    )


def test_a_quiet_suburb_and_a_pool_suburb_do_not_get_the_same_answer():
    """Same house, two markets: one where a pool barely counts, one where it does."""
    quiet = CompEngine(_frame(_market(pool_gap=0.01)))
    rich = CompEngine(_frame(_market(pool_gap=0.09)))
    a = _valued(quiet, pool=True, v4=1_400_000).area_value
    b = _valued(rich, pool=True, v4=1_400_000).area_value
    assert b > a * 1.03, f"{a:,.0f} vs {b:,.0f} — the same pool in two different markets"


def test_when_there_are_too_few_pool_sales_the_comps_are_restated():
    """One pool sale in the suburb is not a comparable set.

    This is the case the user described: nothing like-for-like to compare
    against, so work out what the difference is between the two in that area and
    put the comps on the subject's footing.
    """
    thin = [r for r in _market() if not r["has_swimming_pool"]]
    thin.append(dict(_market()[1], slug_id="one-pool", has_swimming_pool=True))
    eng = CompEngine(pd.DataFrame(thin))
    res = eng.buy_price(**SUBJECT, pool=True, v4_value=1_400_000)
    assert res.pool_basis == "adjusted", res.pool_basis
    assert res.pool_pct is not None and res.pool_pct > 0
    assert res.pool_scope in ("suburb", "district", "type", "default")


def test_a_house_with_no_pool_is_not_lifted_by_a_suburb_full_of_them():
    """The other direction, and the one nobody notices: every comp has a pool."""
    pools_only = [r for r in _market(pool_gap=0.08) if r["has_swimming_pool"]]
    eng = CompEngine(pd.DataFrame(pools_only))
    res = eng.buy_price(**SUBJECT, pool=False, v4_value=1_400_000)
    assert res.pool_basis == "adjusted"
    matched = eng.buy_price(**SUBJECT, pool=True, v4_value=1_400_000 * POOL_PREMIUM)
    assert res.area_value < matched.area_value, (
        "a house with no pool valued as high as the same house with one, off "
        "comps that all had pools"
    )


def test_a_suburb_with_no_pools_anywhere_is_left_exactly_as_it_was():
    """No pool on the subject, none in the comps — nothing may change."""
    eng = CompEngine(pd.DataFrame(_market(pools=False)))
    res = eng.buy_price(**SUBJECT, pool=False, v4_value=1_400_000)
    assert res.pool_basis == "n/a"
    assert res.pool_pct is None


def test_the_pool_is_priced_once_not_twice():
    """The hedonic adds a flat premium; the comps now price it from local sales.

    With every comp carrying a pool and selling 8% above its council valuation,
    a pool subject must come back at what those houses actually sold for — not
    at that plus another 2.9%.
    """
    pools_only = [r for r in _market(pool_gap=0.08) if r["has_swimming_pool"]]
    eng = CompEngine(pd.DataFrame(pools_only))
    row = next(r for r in pools_only if r["beds"] == 4 and r["floor_area_m2"] == 200)
    v4 = CompEngine._v4(row)          # the model's pool-free value for this house
    res = eng.buy_price(**SUBJECT, pool=True, v4_value=v4 * POOL_PREMIUM)
    sold_at = row["sale_price"]
    assert res.area_value == pytest.approx(sold_at, rel=0.03), (
        f"valued at {res.area_value:,.0f} where the same house with a pool sold "
        f"for {sold_at:,.0f}"
    )


def test_no_comps_at_all_still_returns_a_price():
    eng = CompEngine(pd.DataFrame(_market(per_cell=1)))
    res = eng.buy_price(suburb="Nowhere", district="Nowhere", property_type="House",
                        beds=4, baths=2, land=600, asking=1_000_000,
                        v4_value=1_000_000, cv=1_000_000, pool=True)
    assert res.buy_price and res.buy_price > 0
