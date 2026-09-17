"""An unrecorded build year is not a new build.

The floor area has been imputed when missing for a while, and the comment doing
it cites the spec's "age → default 30 if missing". Age never was: it reached the
feature vector as `float(age or 0)`, so a house whose build year the feed did
not carry was valued as though it had been finished this year.

That is not a neutral default. The age coefficient is POSITIVE in this market —
holding CV, floor, land and rooms constant, an eighty-year-old house prices
about 6% above an identical new one, which is the villa-and-bungalow premium
showing up in the data. So "unknown" being read as "brand new" marked those
homes DOWN by around 2%, and did it on the listings we know least about.
"""
from __future__ import annotations

from app.pricing.glm import DEFAULT_AGE_YEARS, predict

BASE = dict(suburb="Remuera", district="Auckland City", property_type="House",
            cv=1_200_000.0, floor=180.0, land=600.0, beds=4.0, baths=2.0,
            cars=2.0, title="Freehold", method=None, pool=False,
            address="1 Test Street")


def test_a_missing_build_year_is_not_valued_as_brand_new():
    missing = predict(age=None, **BASE).pred_v35
    brand_new = predict(age=0, **BASE).pred_v35
    assert missing != brand_new, (
        "a house with no recorded build year is being valued as though it was "
        "finished this year"
    )


def test_a_missing_build_year_uses_the_documented_default():
    assert predict(age=None, **BASE).pred_v35 == predict(age=DEFAULT_AGE_YEARS,
                                                         **BASE).pred_v35


def test_a_recorded_build_year_is_still_used():
    """The fix must not flatten a real age into the default."""
    values = {a: predict(age=a, **BASE).pred_v35 for a in (0, 30, 80)}
    assert len(set(values.values())) == 3, values


def test_a_blank_build_year_reads_the_same_as_an_absent_one():
    """pandas hands over NaN, not None, for an empty cell."""
    assert (predict(age=float("nan"), **BASE).pred_v35
            == predict(age=None, **BASE).pred_v35)


def test_the_default_is_a_plausible_house_age():
    assert 10 <= DEFAULT_AGE_YEARS <= 60
