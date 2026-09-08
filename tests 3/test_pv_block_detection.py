"""pv_lookup_status must tell a real block (403/429) apart from an address
CoreLogic simply has no record for — the difference between 'we're rate-limited'
and 'that address isn't in their database'."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("SEED_ADMIN_EMAIL", "a@b.co")
os.environ.setdefault("SEED_ADMIN_PASSWORD", "pw")

import pytest

import app.propertyvalue as pv  # noqa: E402


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Client:
    """Stand-in for httpx.Client. `script` maps a URL substring → _Resp."""
    def __init__(self, script):
        self._script = script

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        for frag, resp in self._script.items():
            if frag in url:
                return resp
        return _Resp(200, {})


def _patch(monkeypatch, script):
    """Swap in a scripted client — and clear the cached one first.

    The module now holds ONE pooled connection for the whole run instead of
    opening a fresh client per address (2,141 TLS handshakes on a single load
    was a large part of why 37% of lookups never arrived). That makes the client
    sticky, so a stub installed by an earlier test would otherwise still be in
    place for this one, and every test after the first would answer with the
    first one's script.
    """
    pv.close_client()
    monkeypatch.setattr(pv.httpx, "Client", lambda *a, **k: _Client(script))
    monkeypatch.setattr(pv.time, "sleep", lambda *_: None)   # retries are real seconds


@pytest.fixture(autouse=True)
def _drop_pooled_client():
    """...and never leave a scripted client behind for another test file."""
    pv.close_client()
    yield
    pv.close_client()


def test_403_on_suggestions_is_blocked(monkeypatch):
    _patch(monkeypatch, {"suggestions": _Resp(403)})
    rec, status = pv.pv_lookup_status("6 Cassino Terrace, Mount Albert")
    assert rec is None and status == pv.PV_BLOCKED


def test_429_is_blocked(monkeypatch):
    _patch(monkeypatch, {"suggestions": _Resp(429)})
    _, status = pv.pv_lookup_status("x")
    assert status == pv.PV_BLOCKED


def test_200_but_no_suggestion_is_not_found(monkeypatch):
    _patch(monkeypatch, {"suggestions": _Resp(200, {"suggestions": []})})
    rec, status = pv.pv_lookup_status("nowhere road")
    assert rec is None and status == pv.PV_NOT_FOUND


def test_block_on_the_property_fetch_is_blocked(monkeypatch):
    _patch(monkeypatch, {
        "suggestions": _Resp(200, {"suggestions": [{"propertyId": "42", "url": "/p/42"}]}),
        "properties/42": _Resp(429),
    })
    _, status = pv.pv_lookup_status("somewhere")
    assert status == pv.PV_BLOCKED


def test_full_hit_is_ok(monkeypatch):
    _patch(monkeypatch, {
        "suggestions": _Resp(200, {"suggestions": [
            {"propertyId": "42", "suggestion": "6 Cassino Terrace", "url": "/p/42",
             "suburbName": "Mount Albert"}]}),
        "properties/42": _Resp(200, {"ratingValuation": {"capitalValue": 1250000}}),
    })
    rec, status = pv.pv_lookup_status("6 Cassino Terrace")
    assert status == pv.PV_OK
    assert rec["cv"] == 1250000
