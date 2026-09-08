"""The cache check has to work on whatever the driver hands back.

The column is DateTime(timezone=True). Postgres returns an aware datetime from
it; SQLite returns a naive one. Subtracting a naive datetime from an aware one
raises TypeError, so the moment a property had been checked once, this endpoint
answered 500 — everywhere except production. A browser test walking onto a
property page is what surfaced it:

    TypeError: can't subtract offset-naive and offset-aware datetimes

Same shape as the trend endpoint's timestamp bug: code that only ever ran
against one database, written as though every database answers the same way.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import BatchType, ImportBatch, PropertyForSale
from app.routers.properties import external_estimates


@pytest.fixture()
def listing(db_session):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="live.csv", rows_total=1, is_active=True,
                    status="published")
    db_session.add(b); db_session.flush()
    p = PropertyForSale(import_batch_id=b.id, region="Auckland", suburb="Mount Eden",
                        address="1 Cache Street", asking_price=1_000_000,
                        floor_area_m2=120, property_type="House", is_held=False)
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    return p


def test_a_naive_stamp_does_not_take_the_endpoint_down(db_session, listing):
    """The reported crash: checked an hour ago, stored without a timezone."""
    listing.homes_checked_at = datetime.utcnow() - timedelta(hours=1)
    listing.homes_valuation = 1_050_000
    listing.pv_checked_at = datetime.utcnow() - timedelta(hours=1)
    db_session.commit()

    res = external_estimates(property_id=listing.id, db=db_session)
    assert res.homes_valuation == 1_050_000


def test_an_aware_stamp_works_the_same_way(db_session, listing):
    """What Postgres returns. Both drivers, one answer."""
    listing.homes_checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    listing.homes_valuation = 1_050_000
    listing.pv_checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    res = external_estimates(property_id=listing.id, db=db_session)
    assert res.homes_valuation == 1_050_000


def test_a_stale_naive_stamp_is_re_checked_rather_than_raising(db_session, listing,
                                                               monkeypatch):
    """Past the TTL the endpoint goes and looks again — it must not crash first."""
    calls: list[str] = []
    monkeypatch.setattr("app.routers.properties.homes_estimate",
                        lambda addr: calls.append(addr) or None)
    # No test reaches the network; the point here is the comparison, not the fetch.
    monkeypatch.setattr("app.routers.properties.pv_lookup", lambda *a, **k: None)
    listing.homes_checked_at = datetime.utcnow() - timedelta(days=400)
    listing.homes_valuation = 900_000
    db_session.commit()

    external_estimates(property_id=listing.id, db=db_session)
    assert calls, "a stamp older than the cache window was treated as fresh"
