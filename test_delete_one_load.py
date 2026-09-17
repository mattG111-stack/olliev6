"""Delete one uploaded file's data, without reloading everything else.

    "Can you add so I can delete for sale files individually so I don't need to
     reload all the data"

Loading the wrong file used to mean wiping every for-sale batch and starting
again — the only tool was scripts/delete-for-sale-data.py and it takes the lot.
An hour of reloading to undo a five-second mistake.

Three things this has to get right, and all three are the kind that only show up
against a real database:

  ORDER. properties_for_sale.import_batch_id is NOT NULL, so the batch cannot be
  deleted out from under its listings. Wrong order is a constraint violation in
  a request — a 500 with no explanation.

  THE JOB ROWS. ingest_jobs.batch_id is a nullable FK. Those rows are the record
  that a file was uploaded, by whom and when, and that history should outlive
  the data. They are detached, not deleted.

  THE LIVE BATCH. Deleting the active one empties the site. Refusing is useless,
  because the batch you want gone is usually the one you just made live, so the
  previous load is activated in its place and the answer says which.
"""
from __future__ import annotations

import pytest

from app.models import (BatchType, ImportBatch, IngestJob, PropertyForSale,
                        User, UserRole, UserStatus)


def _batch(db, filename, *, active=False, rows=3, btype=BatchType.FOR_SALE.value):
    b = ImportBatch(batch_type=btype, region="Auckland", filename=filename,
                    is_active=active, status="published")
    db.add(b)
    db.flush()
    for i in range(rows):
        db.add(PropertyForSale(import_batch_id=b.id, address=f"{i} {filename} St",
                               suburb="Riverhead", cv_numeric=1_000_000))
    db.add(IngestJob(batch_type=btype, filename=filename, file_size_bytes=1,
                     status="completed", batch_id=b.id))
    db.commit()
    return b


@pytest.fixture()
def admin_client(db_session):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.security import current_user, require_active, require_admin

    admin = User(email="del-admin@test.local", password_hash="x",
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


# ---- the thing that was asked for -----------------------------------------
def test_deleting_one_load_leaves_the_others_alone(admin_client, db_session):
    old = _batch(db_session, "week-1.xlsx", rows=3)
    bad = _batch(db_session, "oops.xlsx", rows=5)

    r = admin_client.delete(f"/api/admin/upload/history/{bad.id}")
    assert r.status_code == 200, r.text
    assert r.json()["rows_deleted"] == 5

    assert db_session.get(ImportBatch, bad.id) is None
    assert db_session.get(ImportBatch, old.id) is not None
    left = db_session.query(PropertyForSale).all()
    assert len(left) == 3
    assert all(p.import_batch_id == old.id for p in left)


def test_the_listings_go_before_the_batch(admin_client, db_session):
    """import_batch_id is NOT NULL. Wrong order is a constraint violation
    inside a request, which reaches the browser as a bare 500."""
    b = _batch(db_session, "one.xlsx", rows=4)
    r = admin_client.delete(f"/api/admin/upload/history/{b.id}")
    assert r.status_code == 200, r.text
    assert db_session.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == b.id).count() == 0


def test_the_upload_history_survives_the_data(admin_client, db_session):
    """The job row is the record that a file was loaded, by whom and when."""
    b = _batch(db_session, "keep-the-record.xlsx")
    r = admin_client.delete(f"/api/admin/upload/history/{b.id}")
    assert r.json()["jobs_detached"] == 1
    job = db_session.query(IngestJob).filter(
        IngestJob.filename == "keep-the-record.xlsx").one()
    assert job.batch_id is None, "the upload history was deleted with the data"


# ---- deleting the live one -------------------------------------------------
def test_deleting_the_live_load_falls_back_to_the_previous_one(admin_client, db_session):
    """Refusing would be useless: the batch you want gone is usually the one you
    just made live."""
    old = _batch(db_session, "good-week.xlsx", active=False)
    live = _batch(db_session, "bad-week.xlsx", active=True)

    d = admin_client.delete(f"/api/admin/upload/history/{live.id}").json()
    assert d["was_active"] is True
    assert d["now_active_batch_id"] == old.id
    assert d["now_active_filename"] == "good-week.xlsx"
    db_session.refresh(old)
    assert old.is_active is True
    assert "re-price" in d["message"]


def test_deleting_a_load_that_is_not_live_says_the_site_is_untouched(admin_client, db_session):
    _batch(db_session, "live.xlsx", active=True)
    spare = _batch(db_session, "spare.xlsx", active=False)
    d = admin_client.delete(f"/api/admin/upload/history/{spare.id}").json()
    assert d["was_active"] is False
    assert d["now_active_batch_id"] is None
    assert "untouched" in d["message"]


def test_deleting_the_only_load_says_the_pages_will_be_empty(admin_client, db_session):
    """Silently emptying the site is how somebody finds out from a customer."""
    only = _batch(db_session, "the-only-one.xlsx", active=True)
    d = admin_client.delete(f"/api/admin/upload/history/{only.id}").json()
    assert d["now_active_batch_id"] is None
    assert "empty" in d["message"]


# ---- refusals --------------------------------------------------------------
def test_deleting_a_load_that_does_not_exist_is_a_404(admin_client):
    assert admin_client.delete("/api/admin/upload/history/999999").status_code == 404


def test_it_needs_an_admin(db_session):
    """It removes data permanently. It is not a read."""
    import inspect

    from app.routers.admin_upload import delete_batch
    from app.security import require_admin

    deps = [p.default for p in inspect.signature(delete_batch).parameters.values()]
    assert any(getattr(d, "dependency", None) is require_admin for d in deps)


def test_sold_and_rent_loads_can_be_deleted_too(admin_client, db_session):
    """Same mistake, same fix. Sold data accumulates so this loses history —
    which is why the answer says how many rows went."""
    b = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                    filename="sold-oops.xlsx", is_active=True, status="published")
    db_session.add(b)
    db_session.commit()
    r = admin_client.delete(f"/api/admin/upload/history/{b.id}")
    assert r.status_code == 200, r.text
    assert r.json()["batch_type"] == "sold"
