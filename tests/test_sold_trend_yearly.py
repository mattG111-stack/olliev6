"""The yearly line, and the year that is not finished yet.

The suburb chart is yearly only now. The monthly series moved on which homes
happened to sell rather than on the market — a suburb sells a handful of homes
a month — and a line that swings 30% between two points reads as a system that
does not know what it is doing.

What is left has to carry two things. The last point is the year IN PROGRESS:
a real median, drawn from part of a year, and the frontend draws it dashed and
says so rather than plotting it as a finished point beside twenty complete ones.
And the change on that last point is what colours the whole chart — green above
last year, red below — so its sign has to follow the market, not the mix.
"""
from __future__ import annotations

import json

from app.models import BatchType, ImportBatch, PropertySold
from app.routers.dashboards import _sold_trend_json


def _seed(db, prices_by_year: dict[int, list[float]], *, last_month: int,
          suburb="Remuera", region="Auckland"):
    """One batch of sales. Every year's sales land in January except the newest
    year's, which stop at `last_month` — that is what makes it a part year."""
    batch = ImportBatch(batch_type=BatchType.SOLD.value, region=region,
                        filename="trend.csv", rows_total=0, is_active=True,
                        status="published")
    db.add(batch); db.flush()
    newest_year = max(prices_by_year)
    n = 0
    for year, prices in prices_by_year.items():
        month = last_month if year == newest_year else 1
        for price in prices:
            n += 1
            db.add(PropertySold(slug_id=f"s{n}", address=f"{n} Test Rd",
                                suburb=suburb, region=region,
                                sale_price=price,
                                sold_date=f"{year:04d}-{month:02d}-15",
                                beds=3, baths=1, floor_area_m2=120,
                                land_area_m2=500, import_batch_id=batch.id))
    batch.rows_total = n
    db.commit()
    return batch


def _years(db, suburb="Remuera", region="Auckland"):
    yearly, _monthly = _sold_trend_json(db, suburb, region)
    assert yearly, "no yearly series was produced"
    return json.loads(yearly)["points"]


def _flat(level: float, n: int = 10):
    return [level + i * 1_000 for i in range(n)]


def test_the_year_in_progress_is_flagged_with_the_month_it_reaches(db_session):
    _seed(db_session, {2023: _flat(1_200_000), 2024: _flat(1_300_000),
                       2025: _flat(1_400_000), 2026: _flat(1_500_000)},
          last_month=8)
    pts = _years(db_session)

    assert [p["year"] for p in pts] == [2023, 2024, 2025, 2026]
    assert pts[-1]["partial"] is True
    assert pts[-1]["through_month"] == 8, "the chart cannot say 'to August' without this"
    assert not any(p.get("partial") for p in pts[:-1]), (
        "a finished year was drawn as the year in progress"
    )


def test_a_year_of_data_that_runs_to_december_is_a_finished_year(db_session):
    _seed(db_session, {2023: _flat(1_200_000), 2024: _flat(1_300_000),
                       2025: _flat(1_400_000)}, last_month=12)
    pts = _years(db_session)
    assert not any(p.get("partial") for p in pts), (
        "a complete year was marked as still in progress"
    )
    assert all(p.get("through_month") is None for p in pts)


def test_a_rising_market_ends_positive_and_a_falling_one_negative(db_session):
    """The sign of the last change is the colour of the chart."""
    _seed(db_session, {2023: _flat(1_200_000), 2024: _flat(1_300_000),
                       2025: _flat(1_400_000), 2026: _flat(1_600_000)},
          last_month=8)
    up = _years(db_session)
    assert up[-1]["change_pct"] > 0, f"a rising market read {up[-1]['change_pct']}%"
    assert up[-1]["median"] > up[-2]["median"]

    _seed(db_session, {2023: _flat(1_200_000), 2024: _flat(1_300_000),
                       2025: _flat(1_400_000), 2026: _flat(1_150_000)},
          last_month=8, suburb="Onehunga")
    down = _years(db_session, suburb="Onehunga")
    assert down[-1]["change_pct"] < 0, f"a falling market read {down[-1]['change_pct']}%"
    assert down[-1]["median"] < down[-2]["median"]


def test_a_thin_year_is_left_off_rather_than_drawn(db_session):
    """Two sales are not a year's median, and the partial flag must not drag one
    onto the chart just because it is the newest."""
    _seed(db_session, {2022: _flat(1_100_000), 2023: _flat(1_200_000),
                       2024: _flat(1_300_000), 2025: _flat(1_400_000),
                       2026: _flat(1_500_000, n=2)}, last_month=8)
    pts = _years(db_session)
    assert [p["year"] for p in pts] == [2022, 2023, 2024, 2025], (
        "a year with two sales was plotted as a median"
    )
