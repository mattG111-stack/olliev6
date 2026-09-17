"""Taking a listing out, as opposed to holding it back.

    "i can look at the listing and have a button too delete them if they arent
     real"

Holding and removing answer different questions, and only one of them existed.

  HOLD means "real, but not for the site". The listing stays in the batch, keeps
  its valuation, counts in the totals and the funnel, and can be released later.

  REMOVE means "this is not a house". A flag would not do: a flagged row still
  sits in the averages, the deal funnel and the export, and the whole point is
  that it is not real. So the row goes.

Because it goes, the run log gets it first — with the address, and who did it.
A deletion nobody can account for afterwards is worse than the bad row was.
"""
from __future__ import annotations

import pytest

from app.models import ImportBatch, PropertyForSale, RunEvent


def _listing(db, address="1 Nowhere Road", **kw):
    batch = ImportBatch(batch_type="for_sale", region="Auckland",
                        filename="week.csv", is_active=True, status="staged")
    db.add(batch); db.flush()
    p = PropertyForSale(address=address, suburb="Epsom", property_type="House",
                        asking_price=1_000_000.0, cv_numeric=1_050_000.0,
                        import_batch_id=batch.id, **kw)
    db.add(p); db.commit()
    return p, batch


def test_a_removed_listing_is_gone_from_the_batch(db_session, monkeypatch):
    from app.routers.release import remove_listing

    p, batch = _listing(db_session)
    pid = p.id

    class _Admin:
        id, email = 1, "matt@example.com"

    out = remove_listing(pid, reason="not a real listing", admin=_Admin(),
                         db=db_session)

    assert out.removed is True
    assert db_session.get(PropertyForSale, pid) is None
    assert (db_session.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch.id).count()) == 0


def test_it_is_written_down_before_it_disappears(db_session):
    """The row stops existing. Something has to say it was here."""
    from app.routers.release import remove_listing

    p, batch = _listing(db_session, address="9 Fake Street")

    class _Admin:
        id, email = 1, "matt@example.com"

    remove_listing(p.id, reason="duplicate of 9A", admin=_Admin(), db=db_session)

    ev = (db_session.query(RunEvent)
          .filter(RunEvent.event == "listing_removed").one())
    assert ev.address == "9 Fake Street"
    assert "9 Fake Street" in ev.detail
    assert "matt@example.com" in ev.detail
    assert "duplicate of 9A" in ev.detail
    assert ev.batch_id == batch.id


def test_removing_one_listing_leaves_the_others(db_session):
    from app.routers.release import remove_listing

    p, batch = _listing(db_session, address="1 Real Road")
    keep = PropertyForSale(address="2 Real Road", suburb="Epsom",
                           property_type="House", asking_price=900_000.0,
                           import_batch_id=batch.id)
    db_session.add(keep); db_session.commit()
    keep_id = keep.id

    class _Admin:
        id, email = 1, "matt@example.com"

    remove_listing(p.id, admin=_Admin(), db=db_session)

    assert db_session.get(PropertyForSale, keep_id) is not None


def test_removing_something_that_is_not_there_is_an_error_not_a_silent_no_op(db_session):
    """A Remove button that reports success on a row it did not touch is worse
    than one that fails."""
    from fastapi import HTTPException

    from app.routers.release import remove_listing

    class _Admin:
        id, email = 1, "matt@example.com"

    with pytest.raises(HTTPException) as e:
        remove_listing(999_999, admin=_Admin(), db=db_session)
    assert e.value.status_code == 404


def test_hold_and_remove_are_not_the_same_thing(db_session):
    """The distinction this exists for. A held listing is still in the batch."""
    from app.routers.release import hold_listing

    p, batch = _listing(db_session)
    hold_listing(p.id, reason="Held by admin", db=db_session)

    db_session.refresh(p)
    assert p.is_held is True
    assert db_session.get(PropertyForSale, p.id) is not None
