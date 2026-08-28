"""CoreLogic can be run on the live batch, not only on a staged one.

    "should i be able to just run corologic"

Yes, and you could not.

`_staged_forsale_batch` filters on status == "staged". Publishing flips a batch
out of that status. So the moment a load went live, bulk enrich became
impossible: the endpoint answered "No staged for-sale batch to enrich" and the
only route left was the per-listing button, one property at a time, on a batch
of eleven thousand.

That is backwards. A published batch with thousands of rows still missing a
floor area or a CV is exactly the batch that needs the lookup most — those rows
are held, unpriced, and sitting in front of customers as gaps.

Staged still wins when both exist: a staged batch is the one about to go live,
and fixing it before publish beats fixing it after.
"""
from __future__ import annotations

import pytest

from app.models import BatchType, ImportBatch
from app.staged_stages import _staged_forsale_batch, enrichable_forsale_batch


def _batch(db, filename, *, status, active=False):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename=filename, status=status, is_active=active)
    db.add(b)
    db.commit()
    return b


def test_the_live_batch_can_be_enriched(db_session):
    """The case that was impossible. A published batch is the one people are
    looking at, and the one whose gaps are visible."""
    live = _batch(db_session, "live.xlsx", status="published", active=True)
    assert _staged_forsale_batch(db_session, "Auckland") is None   # the old rule
    got = enrichable_forsale_batch(db_session, "Auckland")
    assert got is not None and got.id == live.id


def test_a_staged_batch_still_wins_when_there_is_one(db_session):
    """It is the one about to become live. Fixing it before publish beats after."""
    _batch(db_session, "live.xlsx", status="published", active=True)
    staged = _batch(db_session, "incoming.xlsx", status="staged")
    got = enrichable_forsale_batch(db_session, "Auckland")
    assert got.id == staged.id


def test_nothing_staged_and_nothing_live_is_nothing_to_enrich(db_session):
    _batch(db_session, "old.xlsx", status="published", active=False)
    assert enrichable_forsale_batch(db_session, "Auckland") is None


def test_it_does_not_reach_into_another_region(db_session):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Wellington",
                    filename="wgtn.xlsx", status="published", is_active=True)
    db_session.add(b)
    db_session.commit()
    assert enrichable_forsale_batch(db_session, "Auckland") is None


def test_it_does_not_pick_up_a_sold_batch(db_session):
    """Enrich fills floor/land/CV on LISTINGS. A sold batch has none to fill."""
    b = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                    filename="sold.xlsx", status="published", is_active=True)
    db_session.add(b)
    db_session.commit()
    assert enrichable_forsale_batch(db_session, "Auckland") is None


def test_the_endpoint_starts_a_job_against_the_live_batch(db_session, admin_client):
    """Behavioural, because a source-text assertion breaks on rewrapping and a
    test nobody trusts gets deleted."""
    _batch(db_session, "live.xlsx", status="published", active=True)
    r = admin_client.post("/api/admin/release/enrich")
    assert r.status_code == 200, r.text
    assert isinstance(r.json().get("job_id"), int)


def test_the_endpoint_refuses_in_words_when_there_is_nothing_to_enrich(admin_client):
    r = admin_client.post("/api/admin/release/enrich")
    assert r.status_code == 409
    d = r.json()["detail"].lower()
    assert "nothing staged" in d and "nothing live" in d


@pytest.fixture()
def admin_client(db_session):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import current_user, require_active, require_admin

    admin = User(email="enrich-admin@test.local", password_hash="x",
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
