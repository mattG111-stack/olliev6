"""A bad row must cost you that row, never the load.

    "what have you fucked"

Job #82 died on a format string handed a None, and it did not lose a valuation
— it lost a 146 MB ingest, hours of work, on the first listing that reached the
line. That is the shape of failure that matters here: the pricing code runs
inside a loop over every listing in the file, so any exception it raises is not
a bad number in one row, it is the whole load gone.

These tests come from throwing hostile values at the real pipeline — one per
field, then in pairs across the fields the guard chain reads — and keeping what
crashed. Two things did.

The general rule they encode: a value that makes no sense is treated as a value
that was not recorded. Not guessed at, not crashed on.
"""
from __future__ import annotations

import math
import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("SEED_ADMIN_EMAIL", "a@b.co")
os.environ.setdefault("SEED_ADMIN_PASSWORD", "pw")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from app.pricing.comps import SoldDataset  # noqa: E402
from app.pricing.glm import _features  # noqa: E402
from app.pricing.pipeline import run  # noqa: E402


def _sold() -> SoldDataset:
    return SoldDataset(pd.DataFrame([{
        "suburb": "Testville", "district": "Testville", "property_type": "House",
        "key_bedrooms": 3, "key_bathrooms": 1,
        "key_floor_area": f"{140 + i} sqm", "key_land_area": f"{600 + i} sqm",
        "cv_numeric": 900_000, "price_numeric": 950_000 + i * 1_000,
        "sale_price": 950_000 + i * 1_000, "land_value_numeric": 500_000,
        "improvement_value_numeric": 400_000,
        "type_of_title": "Freehold", "sold_date": "2025-06-01",
    } for i in range(14)]))


def _listing(**kw) -> pd.DataFrame:
    base = dict(
        address="1 Example Road", suburb="Testville", district="Testville",
        property_type="House", cv_numeric=900_000, price_numeric=900_000,
        key_floor_area=140, key_land_area=600, key_bedrooms=3,
        key_bathrooms=1, type_of_title="Freehold",
        land_value_numeric=500_000, improvement_value_numeric=400_000,
    )
    base.update(kw)
    return pd.DataFrame([base])


# ---- the crash a negative land area caused ----------------------------------
def test_a_negative_land_area_does_not_end_the_load():
    """log(x + 1) handles a land area of zero and not a negative one, and
    math.log(-4) raises. The two features either side of it — CV and floor area
    — were both written as `if v and v > 0 else 0.0`; this one was written as a
    +1 shift, and the shift has a hole the guard does not.

    A negative area is a parse artefact, not a small section.
    """
    out = run(_listing(key_land_area=-5), _sold())
    assert len(out) == 1


def test_it_is_treated_as_not_recorded_rather_than_as_zero_land():
    """The fix must agree with the fields beside it, or it is just a different
    wrong answer that happens not to raise."""
    unrecorded = _features(cv=900_000, floor=140, land=None, beds=3, baths=1,
                           cars=0, age=30, title=1, method="")
    negative = _features(cv=900_000, floor=140, land=-5, beds=3, baths=1,
                         cars=0, age=30, title=1, method="")
    assert negative == unrecorded


def test_a_real_land_area_still_goes_through_the_log():
    """Guard on the guard: clamping must not flatten every section to the same
    number, which would silently remove land size from the valuation."""
    small = _features(cv=900_000, floor=140, land=200, beds=3, baths=1,
                      cars=0, age=30, title=1, method="")
    large = _features(cv=900_000, floor=140, land=2_000, beds=3, baths=1,
                      cars=0, age=30, title=1, method="")
    assert small[3] == pytest.approx(math.log(201.0))
    assert large[3] == pytest.approx(math.log(2_001.0))


# ---- the other one: exp() raises rather than returning infinity -------------
def test_a_prediction_that_overflows_is_no_prediction_not_a_crash():
    """math.exp raises OverflowError past about 709, so a runaway feature vector
    takes the load with it. Infinity is the honest answer and the sanity gate
    below it already refuses anything that has run away from the CV — so this
    comes out as no valuation, on the path an absurd-but-finite number already
    took."""
    from app.pricing import glm

    out = run(_listing(cv_numeric=1e12, key_floor_area=1e9,
                       key_land_area=1e9, price_numeric=1e12), _sold())
    assert len(out) == 1
    assert glm  # imported for the reader: the guard under test lives there


# ---- the file check, which was crashing on a row the real file had 107 of ---
def test_checking_a_file_survives_a_row_with_no_floor_area():
    """This was broken in a shipped build and nothing caught it.

    The load aliases app.pricing.assumptions as `A` and calls A.is_vacant_type.
    The preflight copied the call and aliased app.pricing.audit — same letter,
    wrong module — so the last check raised AttributeError instead of answering.

    It stayed hidden because that branch is reached only when EVERY earlier
    check has passed, and no test built a row that got that far with no floor
    area. The real file had 107 of them, so the feature the whole thing exists
    for would have failed on the first file it was pointed at.
    """
    from app.preflight_file import check

    df = pd.DataFrame([dict(
        address="12 Elliot Street", suburb="Remuera", region="Auckland",
        property_type="House", price_numeric=1_000_000.0, cv_numeric=1_050_000.0,
        key_floor_area=None, key_land_area=500.0, key_bedrooms=3)])
    data, counts = check(df, "Auckland")

    assert counts["_total"] == 1
    assert b"REJECTED" in data and b"size-valued" in data


def test_a_section_with_no_floor_area_is_not_rejected_for_it():
    """The other half of the same branch: a section has no floor area because it
    has no building, which is not a defect. Rejecting those would throw away
    every piece of bare land in the file."""
    from app.preflight_file import check

    df = pd.DataFrame([dict(
        address="Lot 4 Example Road", suburb="Remuera", region="Auckland",
        property_type="Section", price_numeric=800_000.0, cv_numeric=820_000.0,
        key_floor_area=None, key_land_area=600.0, key_bedrooms=None)])
    (data, _) = check(df, "Auckland")

    assert b"REJECTED" not in data


# ---- infinity is not a large number -----------------------------------------
def test_an_infinite_cell_costs_that_cell_and_not_the_load():
    """int(inf) and round(inf) raise OverflowError, which was not in any of the
    except clauses. NaN was handled in all three places and infinity in none —
    they mean the same thing here: a cell nobody can read."""
    from app.ingest import _to_float, _to_int
    from app.prior_price import derived_asking

    for v in (float("inf"), float("-inf"), "inf", "-inf"):
        assert _to_int(v) is None
        assert _to_float(v) is None
    assert derived_asking(float("inf")) is None
    assert derived_asking(float("-inf")) is None


def test_ordinary_numbers_still_come_through_those_three():
    """Guard on the guard — a rejection rule that rejects everything is worse
    than the crash it replaced."""
    from app.ingest import _to_float, _to_int
    from app.prior_price import derived_asking

    assert _to_int("3") == 3
    assert _to_float("1250000.5") == 1_250_000.5
    assert derived_asking(1_250_000) == 1_210_000


# ---- the shape of the whole thing -------------------------------------------
@pytest.mark.parametrize("field,value", [
    ("price_numeric", None), ("price_numeric", 0), ("price_numeric", -1),
    ("price_numeric", "POA"), ("price_numeric", float("nan")),
    ("cv_numeric", None), ("cv_numeric", 0), ("cv_numeric", -1),
    ("key_floor_area", None), ("key_floor_area", 0), ("key_floor_area", -5),
    ("key_floor_area", "sqm"),
    ("key_land_area", None), ("key_land_area", 0), ("key_land_area", -5),
    ("key_bedrooms", None), ("key_bedrooms", -1), ("key_bedrooms", 99),
    ("land_value_numeric", -1), ("improvement_value_numeric", -1),
    ("type_of_title", None), ("type_of_title", "???"),
    ("property_type", None), ("property_type", "Unknown Type"),
    ("suburb", None), ("address", None), ("zoning", "???"),
    ("pv_estimate_mid", -1), ("valuation_last_sold_value", -1),
    ("prior_asking_price", -1),
])
def test_one_hostile_field_costs_that_row_and_nothing_else(field, value):
    """The whole point, stated once per field. None of these has to produce a
    valuation — several of them must not. All of them have to come back."""
    out = run(_listing(**{field: value}), _sold())
    assert len(out) == 1
