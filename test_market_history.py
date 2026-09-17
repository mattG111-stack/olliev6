"""The market chart drew nothing over a database full of sales.

Reported as: the pulse tiles read 3,936 active listings, $950k median asking,
42 days, 1,068 underpriced — and directly beneath them, "not enough history
yet". Every one of those tiles is computed from data we hold. So is a trend.

The chart was gated on there being TWO published weeks of live listings, and
only ever looked at live listings. A region loaded with years of settled sales
but one week of listings therefore had nothing to plot, and said so, sitting
under six figures drawn from the very same database.

So it now takes whichever history is longer: weeks of asking prices once there
are enough of them to be a line, and otherwise the sales month by month.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold
from app.routers.dashboards import _MARKET_MONTH_MIN_SALES, market_history


def _week(db, *, n, day, price, region="Auckland"):
    """One published week of live listings."""
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region=region,
                    filename=f"live-{day}.csv", rows_total=n, is_active=True,
                    status="published", published_at=datetime(day.year, day.month, day.day))
    db.add(b); db.flush()
    for i in range(n):
        db.add(PropertyForSale(import_batch_id=b.id, region=region, suburb="Mount Eden",
                               address=f"{i} Live St", asking_price=price + i * 1_000,
                               floor_area_m2=120, property_type="House", is_held=False))
    db.commit()
    return b


def _months_of_sales(db, *, months, per_month, price_at, region="Auckland"):
    """`months` completed months of sales, newest ending last month."""
    b = ImportBatch(batch_type=BatchType.SOLD.value, region=region,
                    filename="sold.csv", rows_total=months * per_month,
                    is_active=True, status="published")
    db.add(b); db.flush()
    today = date.today()
    # Walk back from the most recent COMPLETED month.
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    n = 0
    for back in range(months):
        yy, mm = y, m - back
        while mm < 1:
            mm += 12
            yy -= 1
        for i in range(per_month):
            n += 1
            db.add(PropertySold(slug_id=f"m{n}", address=f"{n} Sold St", suburb="Mount Eden",
                                region=region, sale_price=price_at(back) + i * 1_000,
                                sold_date=f"{yy:04d}-{mm:02d}-15", beds=3, baths=1,
                                floor_area_m2=140, land_area_m2=500,
                                days_on_market=30 + back, property_type="House",
                                import_batch_id=b.id))
    db.commit()
    return b


def test_one_week_of_listings_and_years_of_sales_still_draws(db_session):
    """The reported screen. Six live figures above, and no chart below."""
    db = db_session
    _week(db, n=40, day=date.today(), price=950_000)
    _months_of_sales(db, months=24, per_month=_MARKET_MONTH_MIN_SALES,
                     price_at=lambda back: 1_200_000 - back * 5_000)

    res = market_history(region="Auckland", limit=26, months=36, db=db)
    assert len(res.points) >= 2, (
        "still nothing to plot — the chart says 'not enough history' over 24 "
        "months of sales"
    )
    assert res.basis == "sales"


def test_the_months_come_back_oldest_first_and_labelled(db_session):
    db = db_session
    _months_of_sales(db, months=12, per_month=_MARKET_MONTH_MIN_SALES,
                     price_at=lambda back: 1_000_000)
    res = market_history(region="Auckland", db=db)
    dates = [p.batch_date for p in res.points]
    assert dates == sorted(dates), "the line would run right to left"
    assert all(p.label for p in res.points), "a point with nothing to print under it"
    assert res.points[0].median_asking == 1_000_000 + 4 * 1_000  # median of 9... see below


def test_a_month_still_running_is_left_off(db_session):
    """Its median is real but partial, and next to finished months reads as a fall."""
    db = db_session
    b = _months_of_sales(db, months=6, per_month=_MARKET_MONTH_MIN_SALES,
                         price_at=lambda back: 1_000_000)
    today = date.today()
    for i in range(_MARKET_MONTH_MIN_SALES + 4):
        db.add(PropertySold(slug_id=f"now{i}", address=f"{i} This Month Rd",
                            suburb="Mount Eden", region="Auckland",
                            sale_price=400_000, sold_date=f"{today.year:04d}-{today.month:02d}-02",
                            beds=3, baths=1, floor_area_m2=140, land_area_m2=500,
                            property_type="House", import_batch_id=b.id))
    db.commit()

    res = market_history(region="Auckland", db=db)
    assert all(not p.batch_date.startswith(f"{today.year:04d}-{today.month:02d}")
               for p in res.points), "the month in progress was plotted as finished"
    assert all(p.median_asking != 400_000 for p in res.points)


def test_a_month_with_a_handful_of_sales_is_not_a_median(db_session):
    db = db_session
    _months_of_sales(db, months=8, per_month=2, price_at=lambda back: 1_000_000)
    res = market_history(region="Auckland", db=db)
    assert res.points == [], "two sales in a month were plotted as the market"


def test_enough_weeks_of_listings_win_back(db_session):
    """Once the weekly history is a line of its own, it is the more current answer."""
    db = db_session
    _months_of_sales(db, months=24, per_month=_MARKET_MONTH_MIN_SALES,
                     price_at=lambda back: 1_200_000)
    for w in range(5):
        _week(db, n=30, day=date.today() - timedelta(days=7 * (5 - w)), price=900_000 + w * 10_000)

    res = market_history(region="Auckland", db=db)
    assert res.basis == "listings"
    assert len(res.points) == 5
    assert res.points[-1].median_asking and res.points[-1].median_asking > 900_000


def test_two_weeks_of_listings_still_defer_to_a_longer_sold_history(db_session):
    """Two points is a line between two points. Two years of sales is a trend."""
    db = db_session
    _months_of_sales(db, months=24, per_month=_MARKET_MONTH_MIN_SALES,
                     price_at=lambda back: 1_200_000)
    _week(db, n=30, day=date.today() - timedelta(days=7), price=900_000)
    _week(db, n=30, day=date.today(), price=910_000)

    res = market_history(region="Auckland", db=db)
    assert res.basis == "sales"
    assert len(res.points) > 2


def test_no_data_at_all_is_an_empty_chart_not_an_error(db_session):
    res = market_history(region="Auckland", db=db_session)
    assert res.points == [] and res.basis == "listings"


def test_the_sales_line_carries_days_to_sell_too(db_session):
    db = db_session
    _months_of_sales(db, months=6, per_month=_MARKET_MONTH_MIN_SALES,
                     price_at=lambda back: 1_000_000)
    res = market_history(region="Auckland", db=db)
    assert all(p.median_days_to_sell is not None for p in res.points)


def test_every_delivery_of_sales_counts_not_just_the_newest(db_session):
    """Sold history accumulates — see test_sold_counts_agree."""
    db = db_session
    _months_of_sales(db, months=6, per_month=_MARKET_MONTH_MIN_SALES,
                     price_at=lambda back: 1_000_000)
    _months_of_sales(db, months=6, per_month=_MARKET_MONTH_MIN_SALES,
                     price_at=lambda back: 1_000_000)
    res = market_history(region="Auckland", db=db)
    assert res.points, "a second delivery of the same months emptied the chart"
    assert res.points[0].listing_count == _MARKET_MONTH_MIN_SALES * 2


def test_another_regions_sales_stay_out_of_this_chart(db_session):
    db = db_session
    _months_of_sales(db, months=12, per_month=_MARKET_MONTH_MIN_SALES,
                     price_at=lambda back: 1_000_000)
    res = market_history(region="Wellington", db=db)
    assert res.points == []
