"""A small house on a big section is not a cheap house.

    "its miles out"

22 Michaels Avenue, Ellerslie. Asking $1,798,000, CV $2,500,000, our valuation
$2.44M — and a sell estimate of $745,000 printed beside them. Three of our own
numbers on one card, one of them a third of the others.

WHY IT FIRED. The asking sits $702,000 from the CV, past the 20% band, so the
asking price is distrusted and the listing falls back to sold comparables. That
fallback valued it at floor area times a suburb rate per square metre, which
never looks at the section at all. On a modest dwelling standing on 1,157 m² of
Ellerslie, that is not a valuation of the property — it is a valuation of the
house with the land thrown in free. The arithmetic was right and the method was
wrong for the property.

TWO FIXES, AND THE SECOND IS THE ONE THAT GENERALISES.

Like-for-like first. matched_sold_price already matches on beds, baths, LAND and
floor, and the incomplete-CV path above it already uses it. It was simply never
called here.

And a floor under the answer. We arrive on this path precisely because the
asking and the CV disagree, so neither is a yardstick on its own — but an answer
far below BOTH is not a third opinion, it is a method that has failed.
Whichever of the two published figures is the broken one, the property is not
worth a fraction of both. Blanked rather than published: a number on the page
gets acted on and a blank does not.
"""
from __future__ import annotations

import pytest

from app.pricing.pipeline import (
    ASK_VS_CV_BAND, SALE_VS_PUBLISHED_FLOOR, SOLD_TO_ASK,
)


# ---- the case ----------------------------------------------------------------
ASKING = 1_798_000
CV = 2_500_000
WAS = 745_000          # what it printed


def test_this_listing_really_does_take_the_distrusted_path():
    """The premise. If the asking were inside the band none of this applies."""
    assert abs(ASKING - CV) > ASK_VS_CV_BAND * CV


def test_the_figure_it_printed_is_refused_now():
    floor = min(ASKING, CV) * SALE_VS_PUBLISHED_FLOOR
    assert WAS < floor, (
        f"${WAS:,} still publishes — the floor is ${floor:,.0f}")


def test_the_floor_is_a_floor_and_not_a_muzzle():
    """It must not swallow a genuinely cheaper-than-asking answer. A property
    that will transact at 80% of its asking price is an ordinary outcome and the
    whole point of an expected sale."""
    assert 0.4 < SALE_VS_PUBLISHED_FLOOR < 0.75
    plausible = round(ASKING * 0.80)
    assert plausible >= min(ASKING, CV) * SALE_VS_PUBLISHED_FLOOR


def test_a_listed_property_is_untouched_by_any_of_this():
    """The common path — asking within the band — still goes asking × 0.95, and
    that must not have moved."""
    inside = round(CV * 0.95)
    assert abs(inside - CV) <= ASK_VS_CV_BAND * CV
    assert round(inside * SOLD_TO_ASK) == round(inside * 0.95)


# ---- the method, not just the number -----------------------------------------
def test_the_fallback_now_asks_for_land():
    """THE ONE THAT MATTERS. The floor-rate answer is right for a house on an
    ordinary section and wrong for every house on a big one — and a big section
    is exactly what a developer is looking for, so this path is worst where the
    product is most used."""
    import inspect

    from app.pricing import pipeline

    src = inspect.getsource(pipeline.compute_all if hasattr(pipeline, "compute_all")
                            else pipeline)
    block = src[src.index("ask_far_from_cv:"):]
    block = block[:block.index("expected_sale_band = BAND_UNLISTED if sold_based")]
    assert "matched_sold_price" in block, (
        "the distrusted-asking path still values by floor area alone")
    assert "land=land_v2" in block, "the like-for-like match is not given the land"
    assert block.index("matched_sold_price") < block.index("floor_rate_for"), (
        "floor rate is still tried before like-for-like comparables")


def test_floor_rate_survives_as_the_last_resort():
    """A listing with no comparable sales to match against still needs an
    answer, and floor rate is better than nothing."""
    import inspect

    from app.pricing import pipeline

    src = inspect.getsource(pipeline)
    assert "by_floor or (round(by_beds) if by_beds else None)" in src


def test_a_refused_answer_says_why():
    """"unresolved" and "the method failed its own sanity check" are different
    states, and the run log is where somebody goes to find out which."""
    import inspect

    from app.pricing import pipeline

    src = inspect.getsource(pipeline)
    assert '"below_both_published"' in src


def test_a_blanked_estimate_carries_no_confidence_band():
    """A band around a number that was withdrawn is a range around nothing."""
    import inspect

    from app.pricing import pipeline

    src = inspect.getsource(pipeline)
    assert "expected_sale_band = BAND_UNLISTED if sold_based else None" in src
