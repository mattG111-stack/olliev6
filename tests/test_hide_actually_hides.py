"""What a direct link is allowed to show.

    "Have you got the delete and hide there because I need to be able to see the
     photos of the properties like they're listed but to be able to delete them
     before the public save them if they're wrong?"

Hide worked on every list and on nothing else. GET /api/properties/{id} — the
URL a bookmark, a shared link, a search-engine cache or a guessed number points
at — fetched by primary key and returned whatever it found. Two things walked
straight through:

  A HELD LISTING. An operator presses Hide on a listing that is wrong, it
  disappears from every list on the site, and the direct link still serves it,
  impossible margin and all. Hide did not hide; it filtered.

  A BATCH THAT HAS NEVER GONE LIVE. Staged and preview rows carry ordinary
  sequential ids, so next week's unreviewed data was readable by any signed-in
  customer who typed a number in the address bar. That is worse than publishing
  something wrong — it is publishing without deciding to.

The endpoint now asks the same filtered question the browse list asks, rather
than checking fields itself, so the two cannot drift apart. Preview is unharmed:
_active_batch already resolves to the preview batch for an admin in preview
mode and to the live one for everybody else, so the person doing the review
still sees what they are reviewing.
"""
from __future__ import annotations

import pytest

from app.models import (BatchType, ImportBatch, PropertyForSale, User,
                        UserRole, UserStatus)


@pytest.fixture()
def world(db_session):
    """A live batch holding one good listing and one held one, plus a preview
    batch that has never gone live."""
    live = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                       filename="week.csv", status="published", is_active=True)
    db_session.add(live)
    db_session.flush()
    good = PropertyForSale(
        import_batch_id=live.id, slug_id="g1", address="4 Perfectly Fine Lane",
        suburb="Remuera", property_type="House", asking_price=900_000.0,
        cv_numeric=1_000_000.0, fair_value=1_000_000.0, margin=0.11,
        floor_area_m2=150.0, is_held=False)
    held = PropertyForSale(
        import_batch_id=live.id, slug_id="h1", address="9 Held Back Road",
        suburb="Remuera", property_type="House", asking_price=900_000.0,
        cv_numeric=1_000_000.0, fair_value=1_400_000.0, margin=0.55,
        floor_area_m2=150.0, is_held=True, hold_reason="Hidden by admin")
    preview = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                          filename="next.csv", status="preview", is_active=False)
    db_session.add_all([good, held, preview])
    db_session.flush()
    unpublished = PropertyForSale(
        import_batch_id=preview.id, slug_id="p1", address="1 Not Live Yet Street",
        suburb="Remuera", property_type="House", asking_price=800_000.0,
        cv_numeric=900_000.0, fair_value=1_000_000.0, margin=0.25,
        floor_area_m2=140.0, is_held=False)
    db_session.add(unpublished)
    db_session.commit()
    return {"good": good.id, "held": held.id, "unpublished": unpublished.id}


# ---- what a customer may not reach -----------------------------------------
def test_a_held_listing_is_not_served_by_its_own_link(customer, world):
    """THE BUG. Hide is the operator's answer to "this one is wrong". If the
    link still works, the answer did not take."""
    assert customer.get(f"/api/properties/{world['held']}").status_code == 404


def test_a_batch_that_has_never_gone_live_is_not_readable_by_id(customer, world):
    """Sequential ids mean next week's data is a number away."""
    r = customer.get(f"/api/properties/{world['unpublished']}")
    assert r.status_code == 404, \
        f"unpublished data was served to a customer ({r.status_code})"


def test_an_ordinary_listing_still_opens(customer, world):
    """The counterweight, and the one that matters most: this endpoint is how
    every property page loads. Breaking it to fix the above would be a far
    worse bug than the one being fixed."""
    r = customer.get(f"/api/properties/{world['good']}")
    assert r.status_code == 200, r.text
    assert r.json()["address"] == "4 Perfectly Fine Lane"


def test_an_id_that_does_not_exist_is_still_a_404(customer):
    assert customer.get("/api/properties/999999").status_code == 404


def test_the_link_and_the_list_agree(customer, world):
    """The rule is asked as a query rather than checked field by field so that
    these two can never disagree. Asserted, because "the list hides it but the
    page shows it" is precisely the failure being fixed."""
    listed = {r["id"] for r in customer.get("/api/properties").json()["rows"]}
    for name, pid in world.items():
        reachable = customer.get(f"/api/properties/{pid}").status_code == 200
        assert reachable == (pid in listed), (
            f"{name}: the list and the direct link disagree "
            f"(listed={pid in listed}, reachable={reachable})")


# ---- and what the person doing the review may -------------------------------
def test_an_admin_in_preview_still_sees_the_batch_they_are_reviewing(admin_preview,
                                                                    world):
    """The whole point of preview is looking at next week's listings before
    anyone else can. A fix that shut that off would have removed the feature
    rather than secured it."""
    r = admin_preview.get(f"/api/properties/{world['unpublished']}?preview=1")
    assert r.status_code == 200, \
        f"preview stopped working for the person doing the review: {r.text[:200]}"
    assert r.json()["address"] == "1 Not Live Yet Street"


def test_a_customer_cannot_turn_preview_on_for_themselves(customer, world):
    """preview=1 is a request, not a permission. If asking for it were enough,
    the id check above would be worth nothing."""
    r = customer.get(f"/api/properties/{world['unpublished']}?preview=1")
    assert r.status_code in (403, 404), \
        f"a customer previewed unpublished data ({r.status_code})"


def _client(db_session, user):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.security import current_user, require_active

    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: user,
        require_active: lambda: user,
    }
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture()
def customer(db_session):
    from app import main

    u = User(email="customer@test.local", password_hash="x",
             role=UserRole.USER.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    try:
        yield _client(db_session, u)
    finally:
        main.app.dependency_overrides = {}


@pytest.fixture()
def admin_preview(db_session):
    """An admin asking for preview — through the real header path, because that
    is what decides whether preview is granted."""
    from app import main
    from app.security import create_access_token

    u = User(email="preview-admin@test.local", password_hash="x",
             role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    c = _client(db_session, u)
    c.headers.update({"Authorization": f"Bearer {create_access_token(u.id)[0]}"})
    try:
        yield c
    finally:
        main.app.dependency_overrides = {}
