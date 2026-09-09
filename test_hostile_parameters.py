"""What a stranger can type into the query string.

The smoke test proves every endpoint answers with ordinary input. This is the
other half: the same endpoints with input nobody intended. A public URL is
typed by search-engine crawlers, by link previewers, by people editing the
address bar, and — a week after launch — by someone deliberately looking for a
way in.

The bar is deliberately low and absolute: a 4xx is a fine answer, a 200 is a
fine answer, a 500 is not. A 500 is the app admitting it did not consider the
input; it fills the logs, it tells an attacker which parameter is interesting,
and on a page a customer is reading it is a blank screen.

The one thing asserted beyond "did not fall over" is that a page size cannot be
used to ask for the whole database in a single request, because that is not a
crash — it is a working feature pointed at the wrong target.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

# Values chosen for the specific ways they break things, not for variety:
# quote marks end up in LIKE patterns; the percent sign is a LIKE wildcard that
# turns a search into a full scan; the long string tests column widths; the
# unicode is ordinary in New Zealand addresses and is the thing an ASCII
# assumption trips over.
HOSTILE = [
    "",                       # present but empty — different from absent
    " ",
    "0",
    "-1",
    "999999999999999999999",  # past a 64-bit int
    "1e400",                  # parses to infinity as a float
    "nan",
    "null",
    "'",                      # an unbalanced quote
    '"; DROP TABLE users; --',
    "%",                      # a bare LIKE wildcard
    "%%%%%%%%",
    "../../etc/passwd",
    "<script>alert(1)</script>",
    "Ōkahu Bay",              # a macron, which is ordinary here
    "x" * 2000,               # longer than any column
]

# Parameters worth pointing them at: the ones that reach SQL, arithmetic or a
# column width.
PARAMS = ["region", "suburb", "district", "search", "order_by", "order_dir",
          "page", "page_size", "min_price", "max_price", "min_beds",
          "min_margin", "min_score", "type", "category"]


def _get_routes(app):
    """Every GET route that needs no path parameter — the ones a stranger can
    reach by typing a URL."""
    out = []
    for r in app.routes:
        if not isinstance(r, APIRoute) or "GET" not in r.methods:
            continue
        if "{" in r.path or not r.path.startswith("/api"):
            continue
        out.append(r.path)
    return sorted(set(out))


@pytest.mark.parametrize("value", HOSTILE)
def test_the_deal_list_survives_anything_in_any_parameter(client, value):
    """The listing endpoint is the one with the most parameters reaching the
    most SQL, and it is the page a link points at."""
    for p in PARAMS:
        r = client.get(f"/api/properties?{p}={value}")
        assert r.status_code != 500, (
            f"/api/properties 500'd on {p}={value!r}\n{r.text[:400]}")


@pytest.mark.parametrize("value", HOSTILE)
def test_the_summary_survives_it_too(client, value):
    """The tiles above the list take the same filters. They have to refuse the
    same things, or the tiles describe a different population than the rows."""
    for p in PARAMS:
        r = client.get(f"/api/properties/summary?{p}={value}")
        assert r.status_code != 500, (
            f"/api/properties/summary 500'd on {p}={value!r}\n{r.text[:400]}")


def test_no_get_endpoint_falls_over_on_a_junk_parameter(client):
    """Swept across every reachable GET route rather than the handful anyone
    thought to check. An unknown query parameter should be ignored, not fatal."""
    broken = []
    for path in _get_routes(client.app):
        r = client.get(f"{path}?region=%27&suburb=x&page=-1&page_size=0")
        if r.status_code == 500:
            broken.append((path, r.text[:160]))
    assert not broken, "these fell over on junk input:\n" + "\n".join(
        f"  {p}: {t}" for p, t in broken)


def test_a_page_size_cannot_ask_for_the_whole_database(client):
    """Not a crash — a working feature pointed at the wrong target. One request
    that returns every row is how a public endpoint becomes an outage."""
    r = client.get("/api/properties?page_size=1000000")
    assert r.status_code == 422, (
        f"a request for a million rows was accepted ({r.status_code})")


def test_an_unknown_sort_column_is_refused_rather_than_looked_up(client):
    """order_by reaches getattr on the model. A name that is not a column is a
    500; a name that IS a column but not a sortable one leaks the schema."""
    for bad in ["password_hash", "__class__", "id; DROP TABLE", "notacolumn"]:
        r = client.get(f"/api/properties?order_by={bad}")
        assert r.status_code == 422, f"order_by={bad!r} was not refused ({r.status_code})"


def test_a_negative_page_is_refused(client):
    assert client.get("/api/properties?page=-1").status_code == 422
    assert client.get("/api/properties?page=0").status_code == 422


def test_a_page_number_past_the_end_of_the_universe_is_refused(client):
    """THE BUG THIS SWEEP FOUND. `page` had a floor and no ceiling, so a number
    typed into the address bar went through validation and became an OFFSET:
    page x page_size overflows what the database can hold and the endpoint
    answers 500. Postgres rejects an out-of-range bigint offset exactly as
    SQLite does, so this was a public URL returning a server error to anyone who
    added a few digits."""
    r = client.get("/api/properties?page=999999999999999999999")
    assert r.status_code == 422, f"a vast page number was accepted ({r.status_code})"


def test_the_last_reachable_page_still_works(client):
    """The counterweight: the bound has to leave real paging alone."""
    from app.routers.properties import MAX_PAGE

    assert client.get(f"/api/properties?page={MAX_PAGE}").status_code == 200
    assert client.get(f"/api/properties?page={MAX_PAGE + 1}").status_code == 422


@pytest.fixture(scope="module")
def client():
    """A live batch with enough rows to get past every early return.

    An empty database tests the guard clauses and nothing behind them, which is
    the half that already works.
    """
    import random

    from fastapi.testclient import TestClient

    from app import main
    from app.db import Base, SessionLocal, engine, get_db
    from app.models import (BatchType, ImportBatch, PropertyForSale,
                            PropertySold, User, UserRole, UserStatus)
    from app.security import current_user, require_active, require_admin

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    rng = random.Random(5)

    sold = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                       filename="sold.xlsx", status="published", is_active=True)
    db.add(sold)
    db.flush()
    subs = ["Riverhead", "Glenfield", "Papakura", "Mount Albert"]
    for i in range(120):
        db.add(PropertySold(
            import_batch_id=sold.id, address=f"{i} Sold Street",
            suburb=rng.choice(subs), district="Auckland City",
            property_type="House", type_of_title="Freehold",
            sale_price=900_000 + i * 1000, cv_numeric=950_000.0,
            floor_area_m2=150.0, land_area_m2=600.0, beds=3, baths=2))

    live = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                       filename="week.csv", status="published", is_active=True)
    db.add(live)
    db.flush()
    for i in range(60):
        db.add(PropertyForSale(
            import_batch_id=live.id, slug_id=f"s{i}", address=f"{i} Live Road",
            suburb=rng.choice(subs), district="Auckland City",
            property_type="House", type_of_title="Freehold",
            asking_price=900_000.0, cv_numeric=950_000.0, fair_value=1_000_000.0,
            margin=0.11, confidence="high", comps_used=9,
            floor_area_m2=150.0, land_area_m2=600.0, beds=3, baths=2,
            is_held=False))

    user = User(email="hostile@test.local", password_hash="x",
                role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db.add(user)
    db.commit()

    main.app.dependency_overrides = {
        get_db: lambda: db,
        current_user: lambda: user,
        require_active: lambda: user,
        require_admin: lambda: user,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}
        db.close()
