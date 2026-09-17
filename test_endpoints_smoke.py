"""Every GET endpoint must answer without a 500.

Importing the app proves the routes can be built. It does not prove they run:
a NameError inside a function body only fires when that function is called.
/api/dashboards/value-add-by-district shipped referring to an undefined local,
imported perfectly, and answered 500 for every request until it surfaced in a
production log.

The database must have DATA in it. The first version of this file ran against an
empty one and passed with that exact bug reintroduced, because almost every
endpoint here begins "if there is no active batch, return nothing" and stops
before reaching anything that could break. An empty database tests the guard
clauses and nothing behind them — which is the half that already works.

So there is a live sold batch and a live for-sale batch below, both with enough
rows to get through the early returns and into the real work.

What this asserts is narrow on purpose: not that the numbers are right, only
that the endpoint does not fall over. A 200 passes, so does a 404 or a 422.
A 500 does not.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute


def _seed(db):
    """A live sold batch and a live for-sale batch, with enough rows to matter.

    Sized past the early returns rather than to be realistic: several suburbs
    and districts so the grouping runs, a spread of beds and baths so the room
    comparisons have both sides, and prices near CV so the arm's-length guard
    does not throw everything away before the endpoint sees it.
    """
    import random

    from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold

    rnd = random.Random(11)
    sold = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                       filename="smoke-sold.csv", rows_total=0, is_active=True,
                       status="published")
    live = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                       filename="smoke-live.csv", rows_total=0, is_active=True,
                       status="published")
    db.add_all([sold, live])
    db.flush()

    suburbs = ["Remuera", "Epsom", "Mount Albert"]
    districts = ["Auckland City", "Waitakere City"]
    for i in range(60):
        beds = rnd.choice([2, 3, 4])
        baths = rnd.choice([1, 2])
        floor = 60 + beds * 30
        price = 800_000 + beds * 150_000 + rnd.randint(-40_000, 40_000)
        db.add(PropertySold(
            slug_id=f"smoke-sold-{i}", address=f"{i} Smoke St",
            suburb=suburbs[i % len(suburbs)], district=districts[i % len(districts)],
            region="Auckland", property_type="House", sale_price=price,
            cv_numeric=price * 0.98, beds=beds, baths=baths,
            floor_area_m2=floor, land_area_m2=400 + i, days_on_market=20 + (i % 30),
            sold_date=f"2026-{(i % 6) + 1:02d}-15", sale_method="A - Auction",
            has_swimming_pool=(i % 5 == 0), type_of_title="Freehold",
            import_batch_id=sold.id))
    for i in range(30):
        beds = rnd.choice([2, 3, 4])
        ask = 900_000 + beds * 150_000
        db.add(PropertyForSale(
            slug_id=f"smoke-live-{i}", address=f"{i} Live Ave",
            suburb=suburbs[i % len(suburbs)], district=districts[i % len(districts)],
            region="Auckland", property_type="House", asking_price=ask,
            market_value=ask * 1.05, fair_value=ask * 1.05,
            cv_numeric=ask * 0.97, beds=beds, baths=2,
            floor_area_m2=60 + beds * 30, land_area_m2=500 + i,
            predicted_days=30, import_batch_id=live.id))


def _client(db_session):
    """The app, with auth and the database pointed at the test session."""
    import app.main as main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import (
        current_user,
        require_active,
        require_admin,
    )

    admin = User(email="smoke-admin@test.local", password_hash="x",
                 role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(admin)
    _seed(db_session)
    db_session.commit()

    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: admin,
        require_active: lambda: admin,
        require_admin: lambda: admin,
    }
    main.app.state.dialect = db_session.bind.dialect.name
    try:
        yield TestClient(main.app, raise_server_exceptions=False), admin
    finally:
        main.app.dependency_overrides = {}


@pytest.fixture()
def client(db_session):
    yield from _client(db_session)


def _plain_get_paths():
    """GET routes that need no path parameters and are safe to call."""
    import app.main as main

    out = []
    for r in main.app.routes:
        if not isinstance(r, APIRoute) or "GET" not in r.methods:
            continue
        if "{" in r.path:                      # needs an id we do not have
            continue
        if r.path in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
            continue
        out.append(r.path)
    return sorted(set(out))


# Endpoints that use PERCENTILE_CONT ... WITHIN GROUP, which is Postgres-only
# syntax and a bare syntax error on SQLite. They work in production and cannot
# be exercised here at all — the same gap the market pulse had before it was
# rewritten portably (see dashboards._pulse_portable, whose comment says every
# change to it shipped unverified). Listed rather than silently skipped so the
# cost is visible: these two are the endpoints no local test can reach.
POSTGRES_ONLY = {
    "/api/dashboards/suburb-medians",
}


@pytest.mark.parametrize("path", _plain_get_paths())
def test_get_endpoint_does_not_500(client, path):
    c, _ = client
    if path in POSTGRES_ONLY and c.app.state.dialect != "postgresql":
        pytest.skip(f"{path} uses Postgres-only percentile syntax")
    res = c.get(path)
    assert res.status_code < 500, (
        f"GET {path} -> {res.status_code}\n{res.text[:600]}"
    )
