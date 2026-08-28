"""Look at the batch the way a customer will, before a customer can.

    "I need to be able to view the listings like they are Live but they're not
     Live because I need to see if we've got all the information right and
     pricing because at the moment it's not"

The review grid could not answer that. A grid shows you a batch; it does not
show you what somebody will see. The things that go wrong are things you notice
by LOOKING at the page — a valuation that reads absurdly beside the asking
price, a listing with one photo, a suburb panel built on four sales — and none
of those are visible in a row of numbers.

So the whole site can be pointed at the batch that is in preview. There is
exactly one function deciding which batch a reader sees, _active_batch, and it
now answers "the previewed one" while preview is on.

Two things have to hold, and the second is the one with teeth:

  IT HAS TO BE EVERY PAGE. Thirteen call sites read that function. A preview
  that works on twelve of them is worse than none, because you would be checking
  a page that is half this week and half last and could not tell.

  IT HAS TO BE ADMINS ONLY, AND IT HAS TO REFUSE OUT LOUD. Falling back to live
  data for a caller who asked for preview is the worst outcome available: they
  sign off on the wrong batch, at the last moment anyone could have caught it.
"""
from __future__ import annotations

import pytest

from app.models import ImportBatch, PropertyForSale, User, UserRole, UserStatus
from app.routers.properties import _PREVIEW, _active_batch, preview_mode
from app.security import create_access_token, hash_password


def _batch(db, *, status, is_active, filename, rows=2) -> ImportBatch:
    b = ImportBatch(batch_type="for_sale", region="Auckland", filename=filename,
                    is_active=is_active, status=status, rows_inserted=rows)
    db.add(b); db.flush()
    for i in range(rows):
        db.add(PropertyForSale(address=f"{i} {filename} Road", suburb="Epsom",
                               property_type="House", asking_price=900_000.0,
                               cv_numeric=950_000.0, import_batch_id=b.id))
    db.commit()
    return b


def _user(db, role) -> User:
    u = User(email=f"{role}@example.com", password_hash=hash_password("x"),
             role=role, status=UserStatus.APPROVED.value)
    db.add(u); db.commit(); db.refresh(u)
    return u


@pytest.fixture(autouse=True)
def _reset_preview():
    """The flag is per-request in production. In tests it is per-test, or one
    test leaks preview mode into the next and they all pass for the wrong
    reason."""
    token = _PREVIEW.set(False)
    yield
    _PREVIEW.reset(token)


# ---- which batch a reader sees ----------------------------------------------
def test_off_by_default_a_reader_sees_the_live_batch(db_session):
    live = _batch(db_session, status="published", is_active=True,
                  filename="last-week")
    _batch(db_session, status="preview", is_active=False, filename="this-week")

    assert _active_batch(db_session, "for_sale", "Auckland") == live.id


def test_on_a_reader_sees_the_previewed_batch(db_session):
    _batch(db_session, status="published", is_active=True, filename="last-week")
    coming = _batch(db_session, status="preview", is_active=False,
                    filename="this-week")

    _PREVIEW.set(True)
    assert _active_batch(db_session, "for_sale", "Auckland") == coming.id


def test_with_nothing_in_preview_it_shows_the_live_batch(db_session):
    """Sold data is cumulative and often has no previewed batch of its own.
    Blanking the page in that case would make preview useless for the half of
    the site that reads sold data."""
    live = _batch(db_session, status="published", is_active=True,
                  filename="last-week")

    _PREVIEW.set(True)
    assert _active_batch(db_session, "for_sale", "Auckland") == live.id


def test_going_live_makes_preview_and_live_the_same_thing(db_session):
    """After Go live there is nothing in preview, so the switch stops mattering
    — which is what should happen rather than an empty page."""
    from app.release import publish_release

    _batch(db_session, status="preview", is_active=False, filename="this-week")
    publish_release(db_session, "Auckland")

    _PREVIEW.set(True)
    on = _active_batch(db_session, "for_sale", "Auckland")
    _PREVIEW.set(False)
    off = _active_batch(db_session, "for_sale", "Auckland")
    assert on == off is not None


# ---- who is allowed to ------------------------------------------------------
#
# Through a REQUEST, every one of them. These used to call preview_mode()
# directly and they all passed while the feature was broken in the running
# server — see the note at the bottom of this file. Calling a dependency is not
# the same as depending on it, and the difference is where the bug lived.
def _probe(db, *, preview=None, token=None):
    """One real request through a route that depends on preview_mode."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.routers.properties import _PREVIEW, preview_mode

    app = FastAPI()

    @app.get("/probe")
    def probe_route(on: bool = Depends(preview_mode)):
        return {"returned": on, "endpoint_sees": _PREVIEW.get()}

    app.dependency_overrides[get_db] = lambda: db
    headers = {"Authorization": token} if token else {}
    q = "" if preview is None else f"?preview={preview}"
    return TestClient(app, raise_server_exceptions=False).get("/probe" + q,
                                                              headers=headers)


def test_an_admin_can_turn_it_on(db_session):
    admin = _user(db_session, UserRole.ADMIN.value)
    token, _ = create_access_token(admin.id)

    r = _probe(db_session, preview=1, token=f"Bearer {token}")
    assert r.status_code == 200
    assert r.json() == {"returned": True, "endpoint_sees": True}


def test_the_flag_survives_into_the_endpoint(db_session):
    """The regression a unit test cannot see.

    FastAPI runs a SYNC dependency in a threadpool with a COPY of the request
    context, so a ContextVar set inside one is discarded when it returns and the
    endpoint reads the default. Written `def`, this dependency ran, authorised
    correctly, set the flag — and every endpoint still saw preview as off. The
    page came up empty, with a 200, and nothing in the logs to say so.
    """
    admin = _user(db_session, UserRole.ADMIN.value)
    token, _ = create_access_token(admin.id)

    assert _probe(db_session, preview=1,
                  token=f"Bearer {token}").json()["endpoint_sees"] is True, (
        "the dependency set the flag and the endpoint cannot see it — it is "
        "running in a threadpool with a copied context. Make it `async def`.")


def test_an_ordinary_customer_cannot(db_session):
    """The important one. Unpublished valuations are not for customers."""
    user = _user(db_session, UserRole.USER.value)
    token, _ = create_access_token(user.id)

    assert _probe(db_session, preview=1, token=f"Bearer {token}").status_code == 403


@pytest.mark.parametrize("header", [None, "Bearer", "Bearer nonsense",
                                    "Basic abc", "nonsense"])
def test_an_anonymous_or_broken_token_cannot(db_session, header):
    assert _probe(db_session, preview=1, token=header).status_code == 403


def test_it_refuses_out_loud_rather_than_quietly_showing_live_data(db_session):
    """The worst available outcome is a silent fallback: somebody checks a page
    believing it is the new batch, signs it off, and it was the old one."""
    r = _probe(db_session, preview=1)
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()


def test_a_public_reader_is_not_turned_away(db_session):
    """Preview is an extra on public routes, not a lock on them. Requiring a
    token to browse would take the site down for everyone."""
    r = _probe(db_session)
    assert r.status_code == 200
    assert r.json() == {"returned": False, "endpoint_sees": False}


# ---- it has to be the whole site --------------------------------------------
def test_every_reader_goes_through_the_one_function(db_session):
    """A preview that covers most pages is the bug it is meant to prevent. This
    pins that nothing reads is_active directly to choose a batch."""
    import re

    src = open("app/routers/properties.py").read()
    # Inside _active_batch is the one legitimate use.
    body = src[src.index("def _active_batch"):]
    body = body[:body.index("\n\n\n")]
    others = [ln.strip() for ln in src.splitlines()
              if "is_active.is_(True)" in ln and ln.strip() not in body]
    assert others == [], (
        "these pick a batch without going through _active_batch, so preview "
        "will not reach them:\n  " + "\n  ".join(others))


def test_the_switch_is_on_the_router_so_no_endpoint_can_forget(db_session):
    from app.routers.properties import router

    assert len(router.dependencies) >= 1
