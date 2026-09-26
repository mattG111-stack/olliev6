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

from models import BatchType, ImportBatch, PropertyForSale
from routers.properties import external_estimates


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
    monkeypatch.setattr("routers.properties.homes_estimate",
                        lambda addr: calls.append(addr) or None)
    # No test reaches the network; the point here is the comparison, not the fetch.
    monkeypatch.setattr("routers.properties.pv_lookup", lambda *a, **k: None)
    listing.homes_checked_at = datetime.utcnow() - timedelta(days=400)
    listing.homes_valuation = 900_000
    db_session.commit()

    external_estimates(property_id=listing.id, db=db_session)
    assert calls, "a stamp older than the cache window was treated as fresh"


@pytest.mark.parametrize('cache', [
    {'canonical_address': 'Cache Street, Mount Eden', 'suburb': 'Mount Eden'},
    {'canonical_address': '1 Cache Street, Henderson', 'suburb': 'Henderson'},
    {'cv': 3250000},
    [],
])
def test_wrong_cached_identity_is_not_displayed_or_used_to_fill(db_session, listing, cache):
    import json
    listing.homes_checked_at = listing.pv_checked_at = datetime.now(timezone.utc)
    listing.pv_data = json.dumps(cache)
    listing.pv_cv = 3250000
    listing.pv_estimate_mid = 3100000
    listing.cv_numeric = None
    if isinstance(cache, dict):
        cache['cv'] = 3250000; cache['land_area_m2'] = 1206
        listing.pv_data = json.dumps(cache)
    original = listing.pv_data
    db_session.commit()
    result = external_estimates(property_id=listing.id, db=db_session)
    assert result.pv_cv is None and result.pv_estimate_mid is None
    assert result.pv_gaps == [] and result.pv_discrepancies == []
    db_session.refresh(listing)
    assert listing.cv_numeric is None and listing.land_area_m2 is None
    assert listing.pv_data == original  # Preserve evidence, no destructive cleanup.


def test_matching_cached_identity_can_still_fill_and_display(db_session, listing):
    import json
    listing.homes_checked_at = listing.pv_checked_at = datetime.now(timezone.utc)
    listing.pv_data = json.dumps({'canonical_address': '1 Cache St, Mount Eden',
                                 'cv': 900000, 'land_area_m2': 500})
    listing.pv_cv = 900000
    db_session.commit()
    result = external_estimates(property_id=listing.id, db=db_session)
    assert result.pv_cv == 900000
    db_session.refresh(listing)
    assert listing.cv_numeric == 900000 and listing.land_area_m2 == 500
