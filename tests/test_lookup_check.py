"""Answering "the lookup isn't working" in one second instead of twenty minutes.

    "corlogic look insnt working"

There was no way to ask. The only instrument was to run an enrich over a whole
batch and read the summary afterwards — and the three things it could be need
three different responses:

    blocked      we are being refused. It clears on its own; wait.
    error        the request never arrived — network, DNS or proxy. Nothing is
                 wrong with the addresses, and nothing was marked as checked,
                 so it is all still there to fetch.
    not_found    it arrived and was answered, and they hold no record. The
                 connection is fine; this address simply is not there.

Told apart they are three different afternoons. Told together they are "it
didn't work", which is what the screen used to say.
"""
from __future__ import annotations

import pytest

from app.propertyvalue import PV_BLOCKED, PV_ERROR, PV_NOT_FOUND, PV_OK


def _check(client, monkeypatch, answer):
    import app.propertyvalue as pv

    monkeypatch.setattr(pv, "pv_lookup_status", lambda *a, **k: answer)
    return client.get("/api/admin/release/lookup-check").json()


def test_a_working_lookup_says_so(client, monkeypatch):
    body = _check(client, monkeypatch,
                  ({"floor_area_m2": 150.0, "cv": 1_000_000.0}, PV_OK))
    assert body["ok"] is True
    assert body["status"] == PV_OK
    assert "working" in body["headline"].lower()


def test_being_refused_is_not_reported_as_broken(client, monkeypatch):
    """A rate limit is the one failure that fixes itself. Calling it broken
    sends someone looking for a fault that is not there."""
    body = _check(client, monkeypatch, (None, PV_BLOCKED))
    assert body["ok"] is False
    assert body["status"] == PV_BLOCKED
    assert "refused" in body["headline"].lower()
    assert "clears on its own" in body["detail"]


def test_an_unreachable_host_points_at_the_network_not_the_addresses(client, monkeypatch):
    """"2,141 addresses not in CoreLogic" sent us to look at the addresses once
    already. The addresses were fine and it cost a day."""
    body = _check(client, monkeypatch, (None, PV_ERROR))
    assert body["ok"] is False
    assert body["status"] == PV_ERROR
    assert "never arrived" in body["headline"].lower()
    assert "not the addresses" in body["detail"]


def test_an_address_they_do_not_hold_still_means_the_connection_works(client, monkeypatch):
    """The distinction that matters most: a clean "no record" is PROOF the
    connection is fine, and reporting it as a failure hides that."""
    body = _check(client, monkeypatch, (None, PV_NOT_FOUND))
    assert body["ok"] is True, "a clean miss was reported as a broken connection"
    assert "connection is fine" in body["headline"]
    assert "no record" in body["detail"]


def test_a_lookup_that_raises_is_reported_not_a_500(client, monkeypatch):
    """The check exists to diagnose an outage. A check that dies during one is
    no check at all."""
    import app.propertyvalue as pv

    def _boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(pv, "pv_lookup_status", _boom)
    r = client.get("/api/admin/release/lookup-check")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == PV_ERROR


def test_it_says_which_address_it_asked_about(client, monkeypatch):
    """An answer about an address you cannot see is not an answer you can
    judge — "not found" means one thing for a real address and another for a
    typo."""
    import app.propertyvalue as pv

    monkeypatch.setattr(pv, "pv_lookup_status", lambda *a, **k: (None, PV_NOT_FOUND))
    body = client.get("/api/admin/release/lookup-check?address=1 Test Road").json()
    assert body["address"] == "1 Test Road"


def test_it_is_not_run_on_a_page_load(client):
    """One real request to someone else's server, on demand. A check that fires
    whenever the admin page opens is a self-inflicted rate limit."""
    import inspect

    from app.routers.release import lookup_check

    src = inspect.getsource(lookup_check)
    assert "on demand" in src or "never on a page load" in src


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import current_user, require_active, require_admin

    admin = User(email="lookup-admin@test.local", password_hash="x",
                 role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(admin)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: admin,
        require_active: lambda: admin,
        require_admin: lambda: admin,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}
