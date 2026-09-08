"""Follow the market, not the average of the last few years.

The recency bound was "on or after 1 July 2025" — a date written into the
source. That is a window only on the day it is written. By August 2026 it had
quietly become thirteen months, next year it would be twenty-five, and the
engine would drift further from the market every month while the constant still
looked deliberate.

It is now a rolling window counted back from the NEWEST SALE ON FILE rather than
from today, so it means the same thing against this week's upload or a year-old
export, and a gap in deliveries cannot silently empty the engine.

Two years is the outer bound; the sale/CV ratio reads the last six months first
and only reaches back for a suburb too thin to answer. Measured on 54,573
Auckland house sales, each valued using only sales that came before it:

    6 -> 12 -> 24, need 8       6.81%
    6 months flat               6.80%
    3 -> 6, need 8              6.96%
    raw council CV              7.14%
"""
from __future__ import annotations

import pandas as pd

from app.pricing.buyprice import (
    MIN_ENGINE_SALES,
    RATIO_WINDOWS_MONTHS,
    SOLD_WINDOW_MONTHS,
    CompEngine,
)


def _sales(n, *, months_ago, start=0, suburb="Remuera", price=1_500_000):
    """`n` sales that happened `months_ago` before a fixed newest month."""
    y, m = 2026, 8 - months_ago
    while m < 1:
        m += 12
        y -= 1
    return [{
        "slug_id": f"s{start + i}", "address": f"{start + i} Window Street",
        "suburb": suburb, "district": "Auckland City", "property_type": "House",
        "type_of_title": "Freehold", "sold_date": f"{y:04d}-{m:02d}-15",
        "sale_price": price + i * 1_000, "cv_numeric": 1_500_000,
        "beds": 4, "baths": 2, "cars": 2, "floor_area_m2": 200,
        "land_area_m2": 600, "building_age": 1990,
    } for i in range(n)]


def _frame(*blocks):
    return pd.DataFrame([r for b in blocks for r in b])


def test_the_window_is_counted_from_the_newest_sale_not_from_today():
    """So it means the same thing against an export of any age.

    Counted from today, a file whose newest sale is eight months old would
    produce an empty engine — and an empty engine does not fail, it answers with
    a ratio of exactly 1.0.
    """
    df = _frame(_sales(300, months_ago=14), _sales(300, months_ago=18, start=1000))
    engine = CompEngine(df)
    assert len(engine._by_sub) > 0, (
        "an export whose newest sale is over a year old produced no engine"
    )


def test_sales_older_than_the_outer_bound_are_left_out():
    old = SOLD_WINDOW_MONTHS + 6
    df = _frame(_sales(300, months_ago=1, price=1_500_000),
                _sales(300, months_ago=old, start=1000, price=800_000))
    engine = CompEngine(df)
    kept = sum(len(g) for g in engine._by_sub.values())
    assert kept == 300, f"{kept} sales kept — the {old}-month-old block came through"


def test_a_region_too_thin_to_cut_keeps_everything():
    """Following the market is the goal; having no engine follows nothing."""
    df = _frame(_sales(20, months_ago=1), _sales(20, months_ago=30, start=1000))
    engine = CompEngine(df)
    kept = sum(len(g) for g in engine._by_sub.values())
    assert kept == 40, (
        f"{kept} of 40 sales survived — a thin region was cut down to nothing "
        "rather than left alone"
    )
    assert 40 < MIN_ENGINE_SALES


def test_the_ratio_reads_the_freshest_window_it_can_fill():
    """Six months first, wider only for a suburb that cannot fill it."""
    assert RATIO_WINDOWS_MONTHS[0] == 6
    assert list(RATIO_WINDOWS_MONTHS) == sorted(RATIO_WINDOWS_MONTHS)


def test_a_busy_suburb_is_priced_off_its_recent_sales():
    """Fresh sales at a different level to old ones must move the ratio."""
    df = _frame(
        _sales(200, months_ago=2, price=1_650_000),          # recent: 110% of CV
        _sales(400, months_ago=20, start=1000, price=1_200_000),   # old: 80%
    )
    engine = CompEngine(df)
    ratio, src = engine.shrunk_cv_ratio(suburb="Remuera", district="Auckland City",
                                        property_type="House")
    assert ratio > 1.0, (
        f"ratio {ratio:.3f} from {src} — a suburb selling at 110% of CV for the "
        "last two months is being priced off sales from two years ago"
    )
