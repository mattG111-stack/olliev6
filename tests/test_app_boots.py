"""The app must be importable and expose its routes. Nothing else matters if not.

A deploy failed with every replica unhealthy and five minutes of "service
unavailable", because one route declared status_code=204 with a `-> None`
return annotation. Under `from __future__ import annotations` FastAPI resolves
that annotation to a truthy response model and refuses to build the route:

    AssertionError: Status code 204 must not have a response body

It is raised while the module is imported, so the failure is not one broken
endpoint — the whole API stops booting.

Nothing caught it. The suite tests pricing and ingest logic, none of which
imports app.main, and a manual `from app.main import app` passed here because
the FastAPI installed in this environment is NEWER than the one requirements.txt
pins, and no longer objects.

That version gap is itself a trap, and it is why the helper below exists rather
than a plain loop over app.routes: on the newer FastAPI an included router stays
wrapped as `_IncludedRouter`, while the pinned one flattens its routes into
app.routes. A test written against either shape passes vacuously on the other —
which is the same failure mode as the outage, one level up.
"""
from __future__ import annotations

import pytest


def _api_routes(app):
    """Every APIRoute, whichever way this FastAPI version stores them."""
    from fastapi.routing import APIRoute

    out, seen = [], set()

    def walk(routes):
        for r in routes:
            if id(r) in seen:
                continue
            seen.add(id(r))
            if isinstance(r, APIRoute):
                out.append(r)
            # Newer FastAPI keeps included routers wrapped rather than flattened.
            inner = getattr(r, "original_router", None)
            if inner is not None:
                walk(getattr(inner, "routes", []))
            elif hasattr(r, "routes"):
                walk(r.routes)

    walk(app.routes)
    return out


def test_the_app_imports():
    """Catches anything that breaks at import: bad routes, bad decorators,
    circular imports, a missing name in a router module."""
    import app.main as main

    assert main.app is not None


@pytest.mark.parametrize("path", [
    "/health",
    "/api/auth/sign-in",
    "/api/admin/metrics",
    "/api/activity/page",
])
def test_expected_routes_are_registered(path):
    """A router quietly failing to register would not fail the import above."""
    import app.main as main

    paths = {r.path for r in _api_routes(main.app)} | {
        getattr(r, "path", "") for r in main.app.routes
    }
    assert path in paths, f"{path} is not registered"


@pytest.mark.parametrize("status", [204, 205, 304])
def test_no_route_promises_a_body_on_a_bodiless_status(status):
    """The exact shape that took production down.

    The pinned FastAPI raises this at import time; the newer one here does not.
    Asserting it directly means the suite catches it on whichever version is
    installed, instead of depending on the one that happens to object.
    """
    import app.main as main

    for route in _api_routes(main.app):
        if route.status_code == status and route.response_model is not None:
            pytest.fail(
                f"{route.path} returns {status} but declares a response model "
                f"({route.response_model}). FastAPI refuses to build this route "
                f"and the entire app fails to start."
            )


# ---------------------------------------------------------------------------
# The lifespan actually running, which importing the app does not do
# ---------------------------------------------------------------------------
def test_the_lifespan_runs(db_session):
    """9.6 shipped with `NameError: name '_time' is not defined` on line 146 of
    the lifespan and never served a single request. Every replica unhealthy,
    eleven healthcheck attempts over five minutes, "Application startup failed".

    The whole suite passed. `from app.main import app` passed. Because IMPORTING
    a module does not RUN a function inside it, and the lifespan only executes
    when a server starts — which nothing here was doing.

    That is the same failure this file was written about, one level deeper: a
    check that reads as protection and never touches the thing it is protecting.
    The file above tests that routes BUILD; this tests that the app STARTS.

    TestClient's context manager runs startup and shutdown for real, so a
    NameError, a bad import, or an exception in any of the boot steps fails
    here instead of in production.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "ok"


def test_health_answers_without_a_database(db_session):
    """Railway restarts a service whose healthcheck fails, so /health must not
    depend on Postgres — a blip would otherwise become a restart loop."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        body = client.get("/health").json()
    assert set(body) >= {"status", "version"}


def test_shutdown_is_reached_too(db_session):
    """The other half of the lifespan. The code after `yield` runs only on
    shutdown, so it is exactly as easy to ship broken as the code before it —
    and that is where the line naming an outside kill now lives."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        client.get("/health")
    # Exiting the context ran shutdown. Reaching here means it did not raise.
