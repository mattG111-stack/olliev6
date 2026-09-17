"""The suburb panel states which years it is describing, and the caller picks.

Once sold files carry decades, "the median sale price in this suburb" is not a
question with one answer. A median over 2026 and a median over 2022-2026 are
both correct and materially different, and the panel used to show one without
saying which.

The default is the newest year IN THE DATA rather than the calendar year: a
dataset loaded in January, or loaded late, would otherwise open on a year it
holds nothing for and read as an empty suburb.
"""
from __future__ import annotations

import random

import pytest

from app.models import BatchType, ImportBatch, PropertySold
from app.routers.properties import suburb_stats

REGION = "Auckland"
SUBURB = "Remuera"
# Price level per year, so a wider window demonstrably moves the median.
LEVELS = {2022: 0.80, 2023: 0.86, 2024: 0.90, 2025: 0.95, 2026: 1.00}


@pytest.fixture()
def suburb_with_history(db_session):
    db = db_session
    batch = ImportBatch(batch_type=BatchType.SOLD.value, region=REGION,
                        filename="sold.csv", rows_total=0, is_active=True,
                        status="published")
    db.add(batch)
    db.flush()
    rnd = random.Random(3)
    for year, level in LEVELS.items():
        for i in range(60):
            beds = rnd.choice([2, 3, 4])
            baths = rnd.choice([1, 2])
            floor = 60 + beds * 30 + rnd.gauss(0, 8)
            land = 300 + rnd.gauss(0, 60)
            price = (300_000 + floor * 4_000 + land * 400
                     + beds * 80_000 + baths * 45_000 + rnd.gauss(0, 30_000)) * level
            db.add(PropertySold(
                slug_id=f"{year}-{i}", address=f"{i} St", suburb=SUBURB,
                region=REGION, sale_price=price, cv_numeric=price * 0.98,
                beds=beds, baths=baths, floor_area_m2=floor, land_area_m2=land,
                property_type="House", sold_date=f"{year}-06-15",
                days_on_market=30, import_batch_id=batch.id))
    db.commit()
    return db


def _stats(db, **kw):
    kw.setdefault("from_year", None)
    kw.setdefault("to_year", None)
    return suburb_stats(suburb=SUBURB, region=REGION, db=db, **kw)


def test_default_is_the_newest_year_in_the_data(suburb_with_history):
    s = _stats(suburb_with_history)
    assert (s.from_year, s.to_year) == (2026, 2026)
    assert s.sold_count == 60
    assert s.years_available == [2026, 2025, 2024, 2023, 2022]


def test_widening_the_window_changes_every_figure(suburb_with_history):
    db = suburb_with_history
    one = _stats(db)
    five = _stats(db, from_year=2022, to_year=2026)

    assert five.sold_count == 300
    # Older years are cheaper here, so a wider window must pull the median down.
    assert five.median_sold < one.median_sold, "the window did not change the median"

    bed_one = next(e for e in one.effects if e.key == "bedroom")
    bed_five = next(e for e in five.effects if e.key == "bedroom")
    assert bed_one.dollars is not None and bed_five.dollars is not None
    assert bed_one.dollars != bed_five.dollars


def test_a_reversed_range_is_read_the_way_it_was_meant(suburb_with_history):
    s = _stats(suburb_with_history, from_year=2026, to_year=2022)
    assert (s.from_year, s.to_year) == (2022, 2026)
    assert s.sold_count == 300


def test_an_open_ended_range_runs_to_the_newest_year(suburb_with_history):
    s = _stats(suburb_with_history, from_year=2022)
    assert (s.from_year, s.to_year) == (2022, 2026)
    assert s.sold_count == 300


def test_a_window_with_no_sales_reports_zero_rather_than_falling_back(suburb_with_history):
    """Silently widening to 'some data' would misreport what was asked for."""
    s = _stats(suburb_with_history, from_year=2030, to_year=2035)
    assert s.sold_count == 0
    assert s.median_sold is None
    assert (s.from_year, s.to_year) == (2030, 2035)
