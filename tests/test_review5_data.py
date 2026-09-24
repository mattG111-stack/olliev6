"""Count visibility and area-unit regressions found during the data audit."""
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import text

from areas import square_metres
from assistant.sql import scope_live_listings, validate, UnsafeQuery
from ingest import _parse_area
from models import ImportBatch, PropertyForSale
from pricing.comps import parse_area_series
from routers.properties import _hide_bad_data


@pytest.mark.parametrize("value, expected", [
    ("1.2 ha", 12000), ("0.5 hectares", 5000), ("2 acres", 8093.7128448),
    ("1,000 sq ft", 92.90304), ("1000 ft²", 92.90304),
    ("1444 sqm", 1444), ("200 m²", 200), ("150 square metres", 150),
    ("approx. 125 m2", 125), (120.5, 120.5),
])
def test_import_and_pricing_agree_on_square_metres(value, expected):
    assert square_metres(value) == pytest.approx(expected)
    assert _parse_area(value) == pytest.approx(expected)
    assert parse_area_series(pd.Series([value])).iloc[0] == pytest.approx(expected)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -20,
    "-20 sqm", "0", "120-150 sqm", "1/2 share", "2 houses", "200 bananas"])
def test_ambiguous_or_invalid_areas_are_not_invented(value):
    assert _parse_area(value) is None
    assert pd.isna(parse_area_series(pd.Series([value])).iloc[0])


@pytest.fixture
def visibility_rows(db_session):
    db = db_session
    active = ImportBatch(batch_type="for_sale", region="Auckland", filename="live.csv", is_active=True)
    inactive = ImportBatch(batch_type="for_sale", region="Auckland", filename="old.csv", is_active=False)
    db.add_all([active, inactive]); db.flush()
    for overrides in [
        {}, {"is_held": True}, {"link_dead_at": datetime(2026, 1, 1)},
        {"sold_seen_at": datetime(2026, 1, 1)}, {"margin": 3},
        {"floor_area_m2": None}, {"cv_numeric": 600000},
        {"import_batch_id": inactive.id},
        {"property_type": "Section", "floor_area_m2": None},
    ]:
        spec = dict(import_batch_id=active.id, address="1 Example Road", suburb="Glen Eden",
                    property_type="House", floor_area_m2=100, asking_price=600000,
                    margin=0.2, is_held=False)
        spec.update(overrides)
        db.add(PropertyForSale(**spec))
    db.commit()
    return db, active.id


@pytest.mark.parametrize("query", [
    "SELECT id FROM properties_for_sale ORDER BY id",
    "WITH homes AS (SELECT * FROM properties_for_sale) SELECT id FROM homes ORDER BY id",
    "SELECT p.id FROM properties_for_sale p JOIN properties_for_sale q ON p.id=q.id ORDER BY p.id",
    'SELECT id FROM "properties_for_sale" ORDER BY id',
    "SELECT id FROM properties_for_sale WHERE id IN (SELECT id FROM properties_for_sale) ORDER BY id",
])
def test_ollie_rows_match_property_page_even_without_model_filters(visibility_rows, query):
    db, active = visibility_rows
    expected = [p.id for p in _hide_bad_data(db.query(PropertyForSale)).filter(
        PropertyForSale.import_batch_id == active).order_by(PropertyForSale.id).all()]
    scoped = scope_live_listings(validate(query), db.bind.dialect)
    assert list(db.execute(text(scoped)).scalars()) == expected
    assert len(expected) == 2


def test_aggregation_scopes_before_counting(visibility_rows):
    db, _ = visibility_rows
    scoped = scope_live_listings(validate("SELECT COUNT(*) FROM properties_for_sale"), db.bind.dialect)
    assert db.execute(text(scoped)).scalar() == 2


@pytest.mark.parametrize("query", [
    "SELECT * FROM public.properties_for_sale",
    'SELECT * FROM "public"."properties_for_sale"',
    "WITH properties_for_sale AS (SELECT 1) SELECT * FROM properties_for_sale",
    "WITH RECURSIVE homes AS (SELECT * FROM properties_for_sale) SELECT * FROM homes",
])
def test_visibility_scope_cannot_be_accidentally_bypassed(query):
    with pytest.raises(UnsafeQuery):
        scope_live_listings(query)


def test_nonlisting_queries_unchanged():
    query = "SELECT COUNT(*) FROM properties_sold"
    assert scope_live_listings(query) == query


def test_future_date_guard_preserves_today_and_legacy_format():
    from periods import future_sale
    today = date(2026, 9, 24)
    assert not future_sale("2026-09-24", today=today)
    assert not future_sale("9/24/2026", today=today)
    assert future_sale("2026-09-25", today=today)
    assert future_sale("9/25/2026", today=today)
    assert not future_sale("unknown", today=today)


def test_future_sale_cannot_move_comparable_window_or_chart_anchor():
    from periods import recent_sales, _period
    from pricing.comps import _within_recency_window
    dates = ["2020-06-01", "2019-06-01", "2099-06-01", None]
    rows = [SimpleNamespace(sold_date=d) for d in dates]
    assert [r.sold_date for r in recent_sales(rows, 2)] == dates[:2] + [None]
    frame = pd.DataFrame({"sold_date": dates})
    kept, dropped = _within_recency_window(frame)
    assert kept.sold_date.tolist() == dates[:2] + [None]
    assert dropped == 1
    assert _period("2099-06-01") is None
    assert frame.sold_date.tolist() == dates  # source remains untouched


def test_only_future_sales_produce_no_comps():
    from periods import recent_sales
    from pricing.comps import _within_recency_window
    rows = [SimpleNamespace(sold_date="2099-06-01")]
    assert recent_sales(rows, 2) == []
    kept, dropped = _within_recency_window(pd.DataFrame({"sold_date": ["2099-06-01"]}))
    assert kept.empty and dropped == 1


def test_thin_buy_price_engine_still_excludes_future_sales():
    from pricing.buyprice import CompEngine
    base = dict(address="1 Test Road", suburb="Remuera", district="Auckland City",
                property_type="House", type_of_title="Freehold", sale_price=1000000,
                cv_numeric=1000000, beds=3, baths=2, cars=1, floor_area_m2=120,
                land_area_m2=500, building_age=1990)
    rows = [dict(base, sold_date="2020-06-01"), dict(base, sold_date="2099-06-01")]
    engine = CompEngine(pd.DataFrame(rows))
    assert sum(len(g) for g in engine._by_sub.values()) == 1


def test_suburb_medians_do_not_default_to_a_future_year(db_session):
    from models import PropertySold
    from routers.properties import suburb_stats
    batch = ImportBatch(batch_type="sold", region="Auckland", filename="dates.csv", is_active=True)
    db_session.add(batch); db_session.flush()
    for when, price in [("2020-06-01", 800000), ("2099-06-01", 5000000)]:
        db_session.add(PropertySold(import_batch_id=batch.id, region="Auckland",
            suburb="Massey", sold_date=when, sale_price=price, floor_area_m2=100))
    db_session.commit()
    stats = suburb_stats("Massey", "Auckland", from_year=None, to_year=None, ptype=None, db=db_session)
    assert stats.years_available == [2020]
    assert stats.median_sold == 800000
    assert stats.sold_count == 1
