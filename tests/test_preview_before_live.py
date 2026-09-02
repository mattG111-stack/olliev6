"""A second look, while the site still shows last week.

    "i need a staging after i publish the data it goes to a staging area that i
     can look at the listing and have a button too delete them if they arent
     real ? then go too live ?"

Publishing was one door and it was one-way. Whatever was wrong with a batch was
wrong in front of customers the instant it was pressed, and the only way back
was to re-upload the file.

There are three states now:

    staged  →  preview  →  published

STAGED is loaded and priced, still being worked on. PREVIEW is the second look:
the batch is finished and inspected as it will appear, and the public site is
STILL SERVING THE PREVIOUS LOAD. PUBLISHED is live.

The one that has to be true above all the others is the middle one. A preview
that quietly went live would be worse than no preview, because it would be
trusted — so most of what is below is about proving that nothing a customer can
see moves until Go live is pressed.
"""
from __future__ import annotations

import pytest

from app.models import ImportBatch, PropertyForSale
from app.release import publish_release, send_to_preview, staged_summary


def _batch(db, *, batch_type="for_sale", status="staged", is_active=False,
           filename="week.csv", rows=3) -> ImportBatch:
    b = ImportBatch(batch_type=batch_type, region="Auckland", filename=filename,
                    is_active=is_active, status=status, rows_inserted=rows)
    db.add(b); db.flush()
    for i in range(rows):
        db.add(PropertyForSale(address=f"{i} Example Road", suburb="Epsom",
                               property_type="House", asking_price=900_000.0,
                               cv_numeric=950_000.0, import_batch_id=b.id))
    db.commit()
    return b


def _live_ids(db) -> set[int]:
    """What the public site would serve: it selects on is_active."""
    return {b.id for b in db.query(ImportBatch)
            .filter(ImportBatch.is_active.is_(True)).all()}


# ---- the thing that must not happen -----------------------------------------
def test_preview_changes_nothing_a_customer_can_see(db_session):
    """The whole point. Last week stays live while this week is checked."""
    live = _batch(db_session, status="published", is_active=True,
                  filename="last-week.csv")
    _batch(db_session, status="staged", filename="this-week.csv")

    before = _live_ids(db_session)
    send_to_preview(db_session, "Auckland")

    assert _live_ids(db_session) == before == {live.id}


def test_the_previewed_batch_is_not_active(db_session):
    _batch(db_session, status="staged")
    send_to_preview(db_session, "Auckland")

    b = (db_session.query(ImportBatch)
         .filter(ImportBatch.status == "preview").one())
    assert b.is_active is False


def test_going_live_is_what_swaps_them(db_session):
    old = _batch(db_session, status="published", is_active=True,
                 filename="last-week.csv")
    new = _batch(db_session, status="staged", filename="this-week.csv")

    send_to_preview(db_session, "Auckland")
    publish_release(db_session, "Auckland")

    db_session.refresh(old); db_session.refresh(new)
    assert new.is_active is True and new.status == "published"
    assert old.is_active is False and old.status == "archived"


# ---- the review has to still work in preview --------------------------------
def test_the_review_grid_still_finds_the_batch_in_preview(db_session):
    """Moving to preview must not take the grid, the Remove button and the
    re-price with it — that is where the second look happens."""
    from app.staged_stages import _staged_forsale_batch

    b = _batch(db_session, status="staged")
    send_to_preview(db_session, "Auckland")

    assert _staged_forsale_batch(db_session, "Auckland").id == b.id


def test_a_listing_can_still_be_removed_in_preview(db_session):
    """What the second look is FOR."""
    from app.routers.release import remove_listing

    b = _batch(db_session, status="staged", rows=3)
    send_to_preview(db_session, "Auckland")
    victim = (db_session.query(PropertyForSale)
              .filter(PropertyForSale.import_batch_id == b.id).first())

    class _Admin:
        id, email = 1, "matt@example.com"

    remove_listing(victim.id, admin=_Admin(), db=db_session)

    assert (db_session.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == b.id).count()) == 2


def test_a_row_removed_in_preview_never_goes_live(db_session):
    """The removal has to survive the promotion, or the second look is theatre."""
    from app.routers.release import remove_listing

    b = _batch(db_session, status="staged", rows=3)
    send_to_preview(db_session, "Auckland")
    victim = (db_session.query(PropertyForSale)
              .filter(PropertyForSale.import_batch_id == b.id).first())
    gone = victim.address

    class _Admin:
        id, email = 1, "matt@example.com"

    remove_listing(victim.id, admin=_Admin(), db=db_session)
    publish_release(db_session, "Auckland")

    addresses = {p.address for p in db_session.query(PropertyForSale)
                 .filter(PropertyForSale.import_batch_id == b.id).all()}
    assert gone not in addresses
    assert len(addresses) == 2


# ---- the page has to know which button to show ------------------------------
def test_the_summary_says_which_stage_the_batch_is_in(db_session):
    _batch(db_session, status="staged")
    assert staged_summary(db_session, "Auckland").stage == "staged"

    send_to_preview(db_session, "Auckland")
    assert staged_summary(db_session, "Auckland").stage == "preview"


# ---- the edges --------------------------------------------------------------
def test_previewing_twice_is_refused_rather_than_silently_repeated(db_session):
    _batch(db_session, status="staged")
    assert send_to_preview(db_session, "Auckland")["count"] == 1
    assert send_to_preview(db_session, "Auckland")["count"] == 0


def test_going_live_straight_from_staged_still_works(db_session):
    """Preview is a step, not a gate. A build or a script that skips it must
    publish rather than silently do nothing."""
    b = _batch(db_session, status="staged")
    publish_release(db_session, "Auckland")

    db_session.refresh(b)
    assert b.is_active is True and b.status == "published"


def test_the_sold_batch_travels_with_it(db_session):
    """Sold and for-sale are promoted together; they must preview together too,
    or a batch goes live against comps nobody looked at."""
    _batch(db_session, batch_type="sold", status="staged", filename="sold.csv",
           rows=0)
    _batch(db_session, status="staged")

    send_to_preview(db_session, "Auckland")

    assert (db_session.query(ImportBatch)
            .filter(ImportBatch.status == "preview").count()) == 2


# ---- hiding, as opposed to removing -----------------------------------------
def test_a_hidden_listing_does_not_go_live_but_stays_in_the_batch(db_session):
    """The other half of Remove, and the distinction is the point.

    HIDE is for a listing that is real but should not be in front of customers.
    It rides along in the batch, keeps its valuation, still counts, and can be
    released later. REMOVE is for a listing that is not a house at all.

    Both have to survive Go live, or the second look is decoration.
    """
    from app.routers.release import hold_listing

    b = _batch(db_session, status="staged", rows=3)
    hidden = (db_session.query(PropertyForSale)
              .filter(PropertyForSale.import_batch_id == b.id).first())
    hidden_id = hidden.id

    send_to_preview(db_session, "Auckland")
    hold_listing(hidden_id, reason="Hidden by admin", db=db_session)
    publish_release(db_session, "Auckland")

    still_there = db_session.get(PropertyForSale, hidden_id)
    assert still_there is not None, "hiding must not delete the row"
    assert still_there.is_held is True, "it went live after being hidden"
    # And the rest of the batch did go live.
    assert (db_session.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == b.id,
                    PropertyForSale.is_held.is_(False)).count()) == 2


def test_a_hidden_listing_can_be_put_back(db_session):
    """Reversible, which is the whole difference from Remove."""
    from app.routers.release import hold_listing, publish_listing

    b = _batch(db_session, status="staged", rows=1)
    p = (db_session.query(PropertyForSale)
         .filter(PropertyForSale.import_batch_id == b.id).one())

    hold_listing(p.id, reason="Hidden by admin", db=db_session)
    publish_listing(p.id, db=db_session)

    db_session.refresh(p)
    assert p.is_held is False
    assert p.hold_reason is None
