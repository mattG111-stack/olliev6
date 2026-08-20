"""One kind of home at a time.

A suburb median across houses AND apartments moves when the MIX of what sold
changes, not when the market does — measured on generated sales earlier, a
median price line swung 74% in a market held deliberately flat. Holding the
type still is what removes that.

The filter has to match through canonical_type rather than on the raw string.
The source portal serves a Chinese-NZ audience and emits types in Chinese, and
独立屋, 独立别墅 and 独立式住宅 are all houses — a filter comparing raw strings
would quietly return a third of the houses and call it the market.
"""
from __future__ import annotations

import json

from app.models import BatchType, ImportBatch, PropertySold
from app.routers.dashboards import _sold_trend_json
from app.routers.properties import suburb_stats


def _seed(db, rows, *, suburb="Massey", region="Auckland"):
    """rows = [(year, price, raw_property_type), ...]"""
    batch = ImportBatch(batch_type=BatchType.SOLD.value, region=region,
                        filename="types.csv", rows_total=len(rows), is_active=True,
                        status="published")
    db.add(batch); db.flush()
    for i, (year, price, ptype) in enumerate(rows):
        db.add(PropertySold(slug_id=f"t{i}", address=f"{i} Test Rd", suburb=suburb,
                            region=region, sale_price=price,
                            sold_date=f"{year:04d}-06-15", property_type=ptype,
                            beds=3, baths=1, floor_area_m2=120, land_area_m2=400,
                            cv_numeric=price, import_batch_id=batch.id))
    db.commit()
    return batch


def _houses_and_apartments():
    """Houses around $1.2M, apartments around $600k, in both years."""
    rows = []
    for year in (2025, 2026):
        rows += [(year, 1_200_000 + i * 5_000, "House") for i in range(10)]
        rows += [(year, 600_000 + i * 5_000, "Apartment") for i in range(10)]
    return rows


def test_the_house_line_is_not_dragged_down_by_apartments(db_session):
    db = db_session
    _seed(db, _houses_and_apartments())

    mixed, _m, _c = _sold_trend_json(db, "Massey", "Auckland")
    houses, _m, _c = _sold_trend_json(db, "Massey", "Auckland", "House")

    mixed_median = json.loads(mixed)["points"][-1]["median"]
    house_median = json.loads(houses)["points"][-1]["median"]
    assert house_median > mixed_median, (
        f"houses {house_median} did not read above the mixed median {mixed_median}"
    )
    assert house_median >= 1_200_000


def test_chinese_type_names_are_matched_as_houses(db_session):
    """独立屋 and 独立别墅 are both houses. Matching raw strings would miss them."""
    db = db_session
    rows = []
    for year in (2025, 2026):
        rows += [(year, 1_200_000 + i * 5_000, "独立屋") for i in range(5)]
        rows += [(year, 1_250_000 + i * 5_000, "独立别墅") for i in range(5)]
        rows += [(year, 600_000, "公寓") for i in range(9)]      # apartments
    _seed(db, rows)

    houses, _m, _c = _sold_trend_json(db, "Massey", "Auckland", "House")
    assert houses, "no house line was drawn from Chinese type names"
    pts = json.loads(houses)["points"]
    # Ten houses a year clears the bar; the apartments must be nowhere in it.
    assert pts[-1]["count"] == 10, pts[-1]
    assert pts[-1]["median"] >= 1_200_000


def test_asking_for_a_type_with_too_few_sales_draws_nothing(db_session):
    """Better an empty panel than a median of three apartments."""
    db = db_session
    rows = [(y, 1_200_000, "House") for y in (2025, 2026) for _ in range(10)]
    rows += [(2026, 600_000, "Apartment") for _ in range(3)]
    _seed(db, rows)
    apartments, _m, _c = _sold_trend_json(db, "Massey", "Auckland", "Apartment")
    assert apartments is None


def test_the_suburb_figures_follow_the_same_filter(db_session):
    """The stats panel and the chart must not disagree about what is on screen."""
    db = db_session
    _seed(db, _houses_and_apartments())

    everything = suburb_stats(suburb="Massey", region="Auckland", from_year=None,
                              to_year=None, ptype=None, db=db)
    houses = suburb_stats(suburb="Massey", region="Auckland", from_year=None,
                          to_year=None, ptype="House", db=db)

    assert houses.sold_count == 10, houses.sold_count      # one year's houses
    assert everything.sold_count == 20
    assert houses.median_sold > everything.median_sold


def test_an_unknown_type_is_treated_as_no_filter_rather_than_no_data(db_session):
    """A typo in a query string should not empty the page."""
    db = db_session
    _seed(db, _houses_and_apartments())
    blank = suburb_stats(suburb="Massey", region="Auckland", from_year=None,
                         to_year=None, ptype="   ", db=db)
    assert blank.sold_count == 20
