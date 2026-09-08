"""A blank cell must not delete a property from the comp engine.

pandas turns every blank into NaN the moment a column goes through
to_numeric(errors="coerce") — which is what CompEngine does to all of them on
the way in. And NaN is TRUTHY, so `land or 0` handed it straight through to the
feature vector, ln() and exp() carried it, and the prediction came back NaN.
The engine then dropped the row.

The effect was silent and large: every sold record with a blank land area or
bed count was removed from the comparable set without a word. Worse, an engine
emptied that way does not fail — it answers, with a sale/CV ratio of exactly
1.0, which makes every valuation equal the raw council figure while the page
still calls it an estimate.

It surfaced when a fixture of real Auckland sales that carries no bedroom counts
produced a model error identical to raw CV to twelve decimal places. Identical
is not a near miss; it means the model was not running at all.
"""
from __future__ import annotations

import pandas as pd

from app.pricing.buyprice import CompEngine
from app.pricing.glm import predict

NAN = float("nan")

BASE = dict(suburb="Remuera", district="Auckland City", property_type="House",
            cv=1_200_000.0, floor=180.0, land=600.0, beds=4.0, baths=2.0,
            cars=2.0, age=30.0, title="Freehold", method=None, pool=False,
            address="1 Test Street")


def test_a_blank_cell_values_the_same_as_a_missing_one():
    """None and NaN mean the same thing — the data was not recorded."""
    for field in ("land", "beds", "baths", "cars", "age", "floor"):
        as_none = predict(**{**BASE, field: None}).pred_v35
        as_nan = predict(**{**BASE, field: NAN}).pred_v35
        assert as_none == as_nan, (
            f"{field}: None gives {as_none} and NaN gives {as_nan} — "
            "a blank cell and an absent column are the same fact"
        )


def test_a_blank_land_area_still_produces_a_valuation():
    """The specific one. It used to return nothing."""
    assert predict(**{**BASE, "land": NAN}).pred_v35 is not None


def test_no_council_valuation_still_means_no_hedonic():
    """The guard that SHOULD reject: without a CV there is nothing to anchor to."""
    assert predict(**{**BASE, "cv": NAN}).pred_v35 is None
    assert predict(**{**BASE, "cv": None}).pred_v35 is None


def _sales(n=40, **blank):
    rows = []
    for i in range(n):
        r = {"slug_id": f"s{i}", "address": f"{i} Comp Street", "suburb": "Remuera",
             "district": "Auckland City", "property_type": "House",
             "type_of_title": "Freehold", "sold_date": "2026-05-15",
             "sale_price": 1_500_000 + i * 1_000, "cv_numeric": 1_450_000,
             "beds": 4, "baths": 2, "cars": 2,
             "floor_area_m2": 200, "land_area_m2": 600, "building_age": 1990}
        r.update(blank)
        rows.append(r)
    return pd.DataFrame(rows)


def test_sales_with_a_blank_land_area_stay_in_the_comp_engine():
    engine = CompEngine(_sales(land_area_m2=None))
    assert len(engine._by_sub) > 0, (
        "every sale with a blank land area was dropped from the comp engine"
    )


def test_sales_with_no_room_counts_stay_in_the_comp_engine():
    engine = CompEngine(_sales(beds=None, baths=None, cars=None))
    assert len(engine._by_sub) > 0


def test_an_engine_with_comps_does_not_answer_exactly_one():
    """A ratio of exactly 1.0 is the signature of an engine that has nothing.

    It is indistinguishable, to every caller, from a real answer that happens to
    be 1.0 — so the way to catch it is that a populated engine measuring a real
    market almost never lands on it precisely.
    """
    engine = CompEngine(_sales())
    ratio, src = engine.shrunk_cv_ratio(suburb="Remuera", district="Auckland City",
                                        property_type="House")
    assert ratio != 1.0, f"ratio exactly 1.0 from {src} — the engine is empty"
    assert src != "global", "a suburb with forty of its own sales fell back to global"
