"""Four same-sized sales in the suburb beat the whole suburb averaged together.

The like-for-like method existed and was good — 6.94% against actual sold
prices, where the suburb-wide ratio scored 7.55%. It fired on 14% of properties.

The reason was not that comparable sales are rare. Every one of its tiers
required the bed count AND the bath count to match exactly, so a listing missing
either got nothing from it at all and fell straight through to the suburb-wide
ratio. The definition of like-for-like was four conditions wide.

So there is now a last tier that asks only for the same suburb, the same type
and the same size. Measured on 12 months of Auckland house sales, each valued
using only sales that came before it:

    same suburb + floor within 20%, min 4    6.76%   (fires on 90%)
    the suburb-wide ratio                    6.81%   (fires on 100%)

Better than the suburb ratio and available almost everywhere, which is the
combination the strict tiers could not offer. Four is enough: across thresholds
from 2 to 20 the error barely moves, so the lowest count that is still a median
rather than a coin toss is the one that fires most often.
"""
from __future__ import annotations

import pandas as pd

from app.pricing.buyprice import SIZE_MIN_COMPS, SIZE_TOL, CompEngine


def _sales(n, *, floor, beds=4, baths=2, ratio=1.0, start=0):
    return [{
        "slug_id": f"s{start+i}", "address": f"{start+i} Size Street",
        "suburb": "Remuera", "district": "Auckland City", "property_type": "House",
        "type_of_title": "Freehold", "sold_date": "2026-06-15",
        "cv_numeric": 1_500_000, "sale_price": 1_500_000 * ratio + i * 100,
        "beds": beds, "baths": baths, "floor_area_m2": floor,
        "land_area_m2": 600, "building_age": 1990,
    } for i in range(n)]


def _engine(*blocks):
    return CompEngine(pd.DataFrame([r for b in blocks for r in b]))


ASK = dict(suburb="Remuera", district="Auckland City", property_type="House",
           land=600.0, cv=1_500_000.0)


def test_a_bed_count_nothing_matches_no_longer_returns_nothing():
    """The gap. A five-bedroom house in a suburb whose recent sales are all
    four-bedroom used to get NOTHING from this method — every tier filtered to
    an exact bed match first, found an empty set, and gave up. It fell through
    to the whole suburb averaged together.
    """
    eng = _engine(_sales(20, floor=200, beds=4, ratio=1.10))
    value, tier, n = eng.spec_value(beds=5.0, baths=2.0, floor=200.0, **ASK)
    assert value is not None, (
        "a bed count with no exact match got nothing from the like-for-like method"
    )
    assert tier == "size_only" and n >= SIZE_MIN_COMPS


def test_it_prices_off_the_same_size_not_the_whole_suburb():
    """Big houses at 110% of CV, small ones at 90%. A 200m2 subject is not the
    average of the two."""
    eng = _engine(_sales(20, floor=200, beds=4, ratio=1.10),
                  _sales(20, floor=90, beds=2, ratio=0.90, start=100))
    value, tier, _ = eng.spec_value(beds=5.0, baths=2.0, floor=200.0, **ASK)
    assert value is not None and tier == "size_only"
    assert value > 1_500_000 * 1.05, (
        f"valued at {value:,.0f} — a large house was priced off the small ones"
    )


def test_four_is_enough_and_three_is_not():
    assert SIZE_MIN_COMPS == 4
    eng = _engine(_sales(SIZE_MIN_COMPS, floor=200, beds=4, ratio=1.10))
    assert eng.spec_value(beds=5.0, baths=2.0, floor=200.0, **ASK)[0] is not None

    thin = _engine(_sales(SIZE_MIN_COMPS - 1, floor=200, beds=4, ratio=1.10))
    assert thin.spec_value(beds=5.0, baths=2.0, floor=200.0, **ASK)[0] is None


def test_a_different_size_does_not_count_as_a_comp():
    eng = _engine(_sales(20, floor=200 * (1 + SIZE_TOL * 2), beds=4, ratio=1.10))
    assert eng.spec_value(beds=5.0, baths=2.0, floor=200.0, **ASK)[0] is None


def test_the_stricter_tiers_still_win_when_they_can_be_filled():
    """Matching beds, baths and size is better evidence than size alone."""
    eng = _engine(_sales(20, floor=200, beds=4, baths=2, ratio=1.10))
    _v, tier, _n = eng.spec_value(beds=4.0, baths=2.0, floor=200.0, **ASK)
    assert tier == "land_floor", f"fell to {tier} with a full match available"
