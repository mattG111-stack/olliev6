"""What can somebody who has never signed in actually reach?

    "nothing should be public"

Three things were, and none of them were meant to be. All three had the same
shape: not a hole somebody opened, but a default nobody turned off.

  /docs, /redoc, /openapi.json — FastAPI serves these unless you say otherwise.
  The specification was 263 KB describing all 151 endpoints, every admin route,
  every parameter and field name, handed to anyone who typed the address.

  /selftest — a deploy verifier that prints two real properties BY NAME, their
  council valuations, what we value them at, and a paragraph explaining how the
  pricing guard decides. The workings, on the open internet, with addresses.

  /health/ready — 846 bytes naming the database tables, quoting the SQL, and
  listing what was currently broken.

THE TEST THAT MATTERS IS THE FIRST ONE, and it is written the way it is on
purpose. It does not check the three routes above. It walks EVERY route the
application has, works out which ones carry no authentication, and fails on
anything not on a short list that has been thought about one at a time. A test
listing known-bad routes only ever catches the leaks somebody already found;
this one catches the next one, on the day it is added.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as M

# The names of every dependency that means "you must be somebody".
_AUTH = ("require_active", "require_admin", "current_user", "get_current_user",
         "require_promoter", "require_user")

# Routes that are open ON PURPOSE. Every one is here because it cannot work any
# other way, and the reason is written down so the next person to add one has to
# make the same argument rather than just appending a line.
ALLOWED_OPEN = {
    # The platform's health probe cannot sign in.
    "/health": "the deploy platform's liveness probe",
    "/health/ready": "the deploy platform's readiness probe — verdicts only, no detail",
    "/": "a name and nothing else",
    # Deliberately open: the times you most need to know which build is running
    # include the times nobody can sign in. It answers the version and the date
    # and nothing else.
    "/api/version": "which build is running — no data",
    # You cannot require a login on the page where you get one.
    "/api/auth/sign-in": "the front door",
    "/api/auth/sign-up": "the front door",
    # Called by the payment provider's server, which has no account here. It is
    # protected by a signature, not by a session.
    "/api/billing/webhook": "signed callback from the payment provider",
    # A promoter's referral link is followed by a stranger — that is the point.
    "/api/promoter/click": "a referral link is clicked by people with no account",
    # The token IS the credential: Fernet ciphertext only this server can read,
    # naming one photograph. A browser loading an <img> sends no auth header, so
    # a guarded image route is a page of broken pictures.
    "/api/img/{token}": "a browser loading an image sends no auth header",
    "/api/go/{token}": "a browser following a link sends no auth header",
}


def _open_routes() -> dict[str, set[str]]:
    """{path: methods} for every route reachable with no login at all."""
    out: dict[str, set[str]] = {}
    for r in M.app.routes:
        if not hasattr(r, "dependant"):
            continue
        names: set[str] = set()

        def walk(d):
            for dep in d.dependencies:
                if dep.call is not None:
                    names.add(getattr(dep.call, "__name__", ""))
                walk(dep)
            if d.call is not None:
                names.add(getattr(d.call, "__name__", ""))

        walk(r.dependant)
        if not any(a in n for n in names for a in _AUTH):
            out.setdefault(r.path, set()).update(r.methods or {"GET"})
    return out


# ---- the one that catches the NEXT leak, not this one -----------------------
def test_no_endpoint_is_open_unless_it_has_been_argued_for():
    """THE TEST THAT MATTERS.

    Not a list of the three routes that were wrong — that only ever catches
    leaks somebody already found. This walks every route the application has and
    fails on anything reachable without a login that is not in ALLOWED_OPEN.

    If this fails on something you just added: do not add it to the list to make
    the test pass. Put a dependency on it. Add it to the list only if it truly
    cannot work behind one, and write down why.
    """
    unexpected = set(_open_routes()) - set(ALLOWED_OPEN)
    assert not unexpected, (
        "these answer to anybody on the internet with no login:\n  "
        + "\n  ".join(sorted(unexpected)))


def test_every_route_we_allow_open_still_exists():
    """The other direction. A stale allow-list quietly grants permission to
    whatever takes that path next."""
    paths = {r.path for r in M.app.routes if hasattr(r, "dependant")}
    assert not (set(ALLOWED_OPEN) - paths), (
        f"the allow-list names routes that no longer exist: "
        f"{sorted(set(ALLOWED_OPEN) - paths)}")


# ---- and the three that were actually leaking -------------------------------
@pytest.fixture()
def client():
    return TestClient(M.app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_api_is_not_documented_to_the_public(client, path):
    """263 KB describing all 151 endpoints — every admin route, every parameter
    — to anyone who typed the address. A map of the whole system."""
    assert client.get(path).status_code == 404, f"{path} is still published"


def test_the_documentation_can_still_be_turned_on_for_development(monkeypatch):
    """Off by default, not gone. Reading the API locally is a normal thing to
    want, and a rule that cannot be relaxed gets removed instead."""
    import importlib

    from app import config

    monkeypatch.setattr(config.settings, "api_docs", "1")
    reloaded = importlib.reload(M)
    try:
        assert reloaded.app.docs_url == "/docs"
        assert reloaded.app.openapi_url == "/openapi.json"
    finally:
        monkeypatch.setattr(config.settings, "api_docs", "")
        importlib.reload(M)


def test_the_self_test_no_longer_shows_the_workings(client):
    """It printed two real properties by name, their council valuations, what we
    value them at, and how the guard decides."""
    for path in ("/selftest", "/api/selftest"):
        r = client.get(path)
        assert r.status_code in (401, 403), f"{path} answered {r.status_code}"


def test_the_readiness_probe_no_longer_names_the_database(client):
    """It has to stay open — the platform's probe cannot sign in — but which
    check failed is a status, and WHY is a description of the inside."""
    r = client.get("/health/ready")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "ready" in body and "checks" in body
    for c in body["checks"]:
        assert set(c) == {"name", "ok", "fatal"}, (
            f"the probe is publishing {sorted(set(c) - {'name', 'ok', 'fatal'})}")
    text = r.text.lower()
    for leak in ("select ", "table", "sqlite", "postgres", "traceback",
                 "users", "app_settings"):
        assert leak not in text, f"the probe still leaks {leak!r}"


def test_an_administrator_can_still_see_why_it_is_not_ready():
    """Hiding the reason from the public must not hide it from the operator, or
    the next outage is debugged with less than we had before."""
    import inspect

    src = inspect.getsource(M.health_ready_detail)
    assert "detail" in src and "require_admin" in inspect.getsource(M)


def test_the_front_page_does_not_advertise_what_is_gone(client):
    r = client.get("/")
    assert "docs" not in r.text, "it points at documentation that now 404s"
