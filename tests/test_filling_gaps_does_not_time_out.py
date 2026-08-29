"""Filling the gaps on new listings, a chunk at a time.

    "[browser] 500 from /api/admin/release/listings/fill: HTTP 500 with no
     response body — the request was cut off before the server answered (it may
     have taken too long)."   — the bug log, four times over

It was never a 500. `fill_pending` looked up as many as 400 pending listings in
one request, each one a real call to the council-record service taking a second
or more, and the gateway closed the connection minutes before the work
finished. What the browser reported as a crash was a timeout.

The endpoint beside this one — the portal sweep — already solved the same
problem by answering immediately and letting the caller come back. This does
the equivalent: a small batch per call, and a cursor saying where to carry on.

The trap in a design like this is the stop condition. "Rows that still need
filling" cannot be it: a row whose lookup FAILED still needs filling, so it
would be picked again by the next call, and the caller would loop on it for
ever — a hang that looks exactly like the timeout being fixed. The cursor is
the high-water mark of what has been SCANNED, needed or not, so it always moves
forward.
"""
from __future__ import annotations

import pytest

from app.models import PortalListing
from app.portals import complete


@pytest.fixture()
def pending(db_session):
    """Sixty pending listings, none of which carry a council record."""
    rows = [
        PortalListing(
            source="oneroof", kind="for_sale", status="pending",
            url=f"https://example.test/{i}",
            address=f"{i} Example Road", suburb="Papakura",
            price_numeric=900_000.0 + i,
        )
        for i in range(60)
    ]
    db_session.add_all(rows)
    db_session.commit()
    return rows


@pytest.fixture()
def never_reachable(monkeypatch):
    """The lookup service is down — the worst case for a cursor, because every
    row still needs filling after it has been tried."""
    calls = {"n": 0}

    def _boom(db, row):
        calls["n"] += 1
        return 0, "unreachable"

    monkeypatch.setattr(complete, "fill_one", _boom)
    return calls


def test_one_call_does_a_bounded_amount_of_work(db_session, pending, never_reachable):
    """THE BUG. 400 lookups in one request is minutes behind a gateway that
    waits seconds."""
    out = complete.fill_pending(db_session, kind="for_sale")
    assert out["scanned"] <= complete.CHUNK
    assert never_reachable["n"] <= complete.CHUNK


def test_the_caller_can_ask_for_more_but_not_unboundedly(db_session, pending,
                                                         never_reachable):
    out = complete.fill_pending(db_session, kind="for_sale", limit=10_000)
    assert out["scanned"] <= complete.MAX_CHUNK


def test_walking_the_cursor_covers_every_row_exactly_once(db_session, pending,
                                                          never_reachable):
    after, seen, rounds = 0, 0, 0
    while rounds < 100:
        rounds += 1
        out = complete.fill_pending(db_session, kind="for_sale", after_id=after)
        if not out["scanned"]:
            break
        assert out["last_id"] > after, "the cursor did not move — this is the hang"
        after = out["last_id"]
        seen += out["scanned"]
    assert seen == 60
    assert never_reachable["n"] == 60, "a row was looked up twice, or skipped"


def test_it_terminates_even_when_every_lookup_fails(db_session, pending,
                                                    never_reachable):
    """The one that matters. Every row still needs filling after being tried,
    so a "rows that need filling" cursor would never finish."""
    after, rounds = 0, 0
    while rounds < 500:
        rounds += 1
        out = complete.fill_pending(db_session, kind="for_sale", after_id=after)
        if not out["scanned"]:
            break
        after = out["last_id"]
    assert rounds < 500, "it never finished — the caller would loop for ever"
    assert rounds <= 5, f"took {rounds} rounds for 60 rows at {complete.CHUNK} a time"


def test_remaining_counts_down(db_session, pending, never_reachable):
    """It drives the progress line. A number that does not fall is worse than
    no number, because it reads as stuck."""
    first = complete.fill_pending(db_session, kind="for_sale")
    second = complete.fill_pending(db_session, kind="for_sale",
                                   after_id=first["last_id"])
    assert first["remaining"] == 60 - first["scanned"]
    assert second["remaining"] < first["remaining"]


def test_the_last_chunk_reports_nothing_left(db_session, pending, never_reachable):
    after = 0
    for _ in range(10):
        out = complete.fill_pending(db_session, kind="for_sale", after_id=after)
        if not out["scanned"]:
            break
        after = out["last_id"]
    assert out["scanned"] == 0
    assert out["remaining"] == 0


def test_an_empty_queue_answers_immediately(db_session):
    out = complete.fill_pending(db_session, kind="for_sale")
    assert out == {
        "looked_up": 0, "fields_filled": 0, "council_records_found": 0,
        "not_found": 0, "blocked": 0, "unreachable": 0,
        "last_id": 0, "remaining": 0, "scanned": 0,
    }


def test_rows_already_complete_are_scanned_but_not_looked_up(db_session,
                                                             never_reachable):
    """A lookup we do not need is a second of somebody's afternoon and a call
    against the allowance — but the row still has to advance the cursor, or the
    walk stalls on it."""
    done = PortalListing(
        source="oneroof", kind="for_sale", status="pending",
        url="https://example.test/full", address="1 Complete Way",
        suburb="Papakura", price_numeric=1_000_000.0,
        # Every field in complete._FILLABLE, so needs_filling() says no.
        cv_numeric=1_100_000.0, land_value_numeric=700_000.0,
        improvement_value_numeric=400_000.0, floor_area_m2=150.0,
        land_area_m2=600.0, beds=3, baths=2, carspaces=1,
        prior_sale_price=880_000.0, prior_sale_date="2019-03-04",
        property_type="House", image_url="https://example.test/p.jpg",
    )
    db_session.add(done)
    db_session.commit()
    out = complete.fill_pending(db_session, kind="for_sale")
    assert out["scanned"] == 1
    assert out["looked_up"] == 0
    assert never_reachable["n"] == 0
    assert out["last_id"] == done.id


def test_a_different_kind_is_left_alone(db_session, pending, never_reachable):
    sold = PortalListing(source="oneroof", kind="sold", status="pending",
                         url="https://example.test/sold",
                         address="9 Sold Street", suburb="Papakura")
    db_session.add(sold)
    db_session.commit()
    after, scanned = 0, 0
    for _ in range(10):
        out = complete.fill_pending(db_session, kind="for_sale", after_id=after)
        if not out["scanned"]:
            break
        after, scanned = out["last_id"], scanned + out["scanned"]
    assert scanned == 60


# ---- through the endpoint ---------------------------------------------------
def _client(db_session, user):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.security import current_user, require_active, require_admin

    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: user,
        require_active: lambda: user,
        require_admin: lambda: user,
    }
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture()
def admin(db_session):
    from app import main
    from app.models import User, UserRole, UserStatus

    u = User(email="ops@test.local", password_hash="x",
             role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    try:
        yield _client(db_session, u)
    finally:
        main.app.dependency_overrides = {}


def test_the_endpoint_hands_back_a_cursor(admin, pending, never_reachable):
    r = admin.post("/api/admin/release/listings/fill?kind=for_sale")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanned"] > 0
    assert body["last_id"] > 0
    assert body["remaining"] == 60 - body["scanned"]


def test_the_endpoint_walks_to_the_end(admin, pending, never_reachable):
    after, rounds, scanned = 0, 0, 0
    while rounds < 50:
        rounds += 1
        body = admin.post(
            f"/api/admin/release/listings/fill?kind=for_sale&after_id={after}").json()
        if not body["scanned"]:
            break
        after, scanned = body["last_id"], scanned + body["scanned"]
    assert scanned == 60
    assert body["remaining"] == 0


def test_a_hostile_limit_is_clamped_not_honoured(admin, pending, never_reachable):
    body = admin.post(
        "/api/admin/release/listings/fill?kind=for_sale&limit=999999").json()
    assert body["scanned"] <= complete.MAX_CHUNK


def test_a_customer_cannot_spend_the_lookup_allowance(db_session, pending):
    """Every call costs money against the council-record account."""
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import current_user, require_active

    u = User(email="punter@test.local", password_hash="x",
             role=UserRole.USER.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: u,
        require_active: lambda: u,
    }
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        assert c.post("/api/admin/release/listings/fill").status_code in (401, 403)
    finally:
        main.app.dependency_overrides = {}
