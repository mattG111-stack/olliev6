"""Publishing must promote the load that HOLDS the sales, not the newest row.

What happened. A sold file was uploaded, staged, and not yet published. The
same file was uploaded again a minute later. The second load did what it is
supposed to do — it checked every sale against what was already stored, found
all 16,424 of them on file, and inserted none — and left an empty batch sitting
at the top of the import history.

Publishing took the highest id. So the batch that went live was the empty one.
The sales were still in the table, under the earlier batch, and the screen that
reports what is live read zero. Nothing errored. Nothing in the logs said a
thing. It looks exactly like the database having been wiped, which is the one
thing it was not.

The delete path had its own version of the same fault: deleting the live batch
promoted "the most recent remaining batch", regardless of whether that batch had
ever been published. A staged file nobody had previewed could go live because a
different file was deleted.
"""
from __future__ import annotations

import pytest
from sqlalchemy import desc

from models import ImportBatch, PropertySold
from release import _staged_batch


def _batch(db, *, status="staged", rows=0, filename="sold.csv",
           batch_type="sold", region="Auckland", is_active=False):
    b = ImportBatch(batch_type=batch_type, region=region, filename=filename,
                    rows_total=rows, rows_inserted=rows, rows_rejected=0,
                    status=status, is_active=is_active)
    db.add(b)
    db.flush()
    return b


# ---- publish ---------------------------------------------------------------
def test_an_empty_re_upload_does_not_become_the_published_batch(db_session):
    """The exact shape of the fault: batch 57 carries the sales, batch 58 is the
    second upload of the same file and carries none."""
    with_data = _batch(db_session, rows=16_424, filename="sold_comps.csv")
    empty = _batch(db_session, rows=0, filename="sold_comps.csv")
    assert empty.id > with_data.id, "the empty one must be the NEWER row"

    chosen = _staged_batch(db_session, "sold", "Auckland")

    assert chosen is not None
    assert chosen.id == with_data.id, (
        f"publish chose batch #{chosen.id} with {chosen.rows_inserted} rows "
        f"over batch #{with_data.id} with {with_data.rows_inserted}. Going live "
        "with the empty one empties the page while the data sits in the table.")


def test_the_newest_still_wins_when_both_have_rows(db_session):
    """This is not 'prefer the biggest'. Two real loads, the later one is the
    one being published — otherwise a re-upload that FIXES a file would be
    ignored in favour of the broken one it replaced."""
    _batch(db_session, rows=9_000, filename="first.csv")
    newer = _batch(db_session, rows=120, filename="corrected.csv")

    chosen = _staged_batch(db_session, "sold", "Auckland")

    assert chosen.id == newer.id
    assert chosen.filename == "corrected.csv"


def test_a_genuinely_empty_load_still_publishes(db_session):
    """If nothing pre-live has any rows, publish the newest anyway. Refusing, or
    reaching further back, would quietly promote last week's file."""
    _batch(db_session, rows=0, filename="old.csv")
    newest = _batch(db_session, rows=0, filename="empty.csv")

    chosen = _staged_batch(db_session, "sold", "Auckland")

    assert chosen is not None and chosen.id == newest.id


def test_a_published_batch_is_never_picked_up_again(db_session):
    """Only pre-live batches are candidates. A published one with rows must not
    out-rank a staged one just because it has more in it."""
    _batch(db_session, rows=50_000, status="published", is_active=True,
           filename="live.csv")
    staged = _batch(db_session, rows=300, filename="this-week.csv")

    chosen = _staged_batch(db_session, "sold", "Auckland")

    assert chosen.id == staged.id


def test_the_other_region_is_not_borrowed_from(db_session):
    _batch(db_session, rows=5_000, region="Wellington", filename="wgtn.csv")
    _batch(db_session, rows=0, region="Auckland", filename="akl.csv")

    chosen = _staged_batch(db_session, "sold", "Auckland")

    assert chosen is not None
    assert chosen.region == "Auckland"


# ---- delete ----------------------------------------------------------------
def _delete(client_db, batch_id):
    """Call the delete path directly — the point here is which batch it
    promotes, not the HTTP wrapper around it."""
    from routers.admin_upload import delete_batch

    return delete_batch(batch_id, None, client_db)


def test_deleting_the_live_load_does_not_promote_an_unreviewed_one(db_session):
    """Preview exists so a file is looked at before customers see it. Falling
    back means going BACK — to something that was live — not sideways onto
    whatever happens to have the highest id."""
    old_live = _batch(db_session, rows=9_000, status="archived",
                      filename="lastweek.csv")
    live = _batch(db_session, rows=8_500, status="published", is_active=True,
                  filename="thisweek.csv")
    unreviewed = _batch(db_session, rows=4, status="staged",
                        filename="oops-wrong-file.csv")
    db_session.commit()

    result = _delete(db_session, live.id)

    assert result.was_active is True
    assert result.now_active_batch_id == old_live.id, (
        f"promoted #{result.now_active_batch_id} — the staged file "
        f"'{unreviewed.filename}' would have gone live without ever being "
        "previewed, because a different load was deleted.")
    assert db_session.get(ImportBatch, unreviewed.id).is_active is False


def test_deleting_a_load_that_was_not_live_changes_nothing(db_session):
    live = _batch(db_session, rows=8_500, status="published", is_active=True,
                  filename="thisweek.csv")
    spare = _batch(db_session, rows=10, status="staged", filename="spare.csv")
    db_session.commit()

    result = _delete(db_session, spare.id)

    assert result.was_active is False
    assert result.now_active_batch_id is None
    assert db_session.get(ImportBatch, live.id).is_active is True


def test_deleting_the_only_load_says_so_rather_than_promoting_nothing(db_session):
    live = _batch(db_session, rows=8_500, status="published", is_active=True,
                  filename="only.csv")
    db_session.commit()

    result = _delete(db_session, live.id)

    assert result.was_active is True
    assert result.now_active_batch_id is None
    assert "no earlier" in result.message


def test_the_rows_go_with_the_batch(db_session):
    """Deleting the batch must take its rows, and only its rows."""
    keep = _batch(db_session, rows=2, status="published", filename="keep.csv")
    drop = _batch(db_session, rows=2, status="staged", filename="drop.csv")
    for b, n in ((keep, "Kept"), (drop, "Dropped")):
        for i in range(2):
            db_session.add(PropertySold(
                import_batch_id=b.id, region="Auckland", suburb="Riverhead",
                address=f"{n} {i} Road", sale_price=1_000_000.0))
    db_session.commit()

    result = _delete(db_session, drop.id)

    assert result.rows_deleted == 2
    left = db_session.query(PropertySold).all()
    assert len(left) == 2
    assert all(r.import_batch_id == keep.id for r in left)


# ---- a listing only leaves when its advertisement will not open --------------
#
# That is the rule everywhere else in the product: the daily link check needs a
# listing missing three days running, one good fetch clears it, and it never
# deletes — it sets link_dead_at, which is reversible. Deleting a load was the
# one thing that could take a house off the site with nobody having opened its
# advertisement.
from datetime import datetime, timezone

from models import PropertyForSale


def _listing(db, batch, address, *, dead=False):
    p = PropertyForSale(
        import_batch_id=batch.id, region="Auckland", suburb="Riverhead",
        address=address, url=f"https://example.test/{address.replace(' ', '-')}",
        asking_price=1_200_000.0,
        link_dead_at=(datetime.now(timezone.utc) if dead else None))
    db.add(p)
    return p


def test_a_listing_still_advertised_survives_its_load_being_deleted(db_session):
    old = _batch(db_session, batch_type="for_sale", status="archived",
                 rows=2, filename="lastweek.csv")
    current = _batch(db_session, batch_type="for_sale", status="published",
                     rows=2, is_active=True, filename="thisweek.csv")
    _listing(db_session, old, "1 Live Road")
    _listing(db_session, old, "2 Gone Road", dead=True)
    db_session.commit()

    result = _delete(db_session, old.id)

    assert result.rows_deleted == 1, "only the retired advertisement goes"
    assert result.rows_kept == 1
    assert result.kept_moved_to_batch_id == current.id
    kept = db_session.query(PropertyForSale).all()
    assert [k.address for k in kept] == ["1 Live Road"]
    assert kept[0].import_batch_id == current.id, (
        "a kept listing must belong to a load — import_batch_id is NOT NULL and "
        "everything in the product filters on it")


def test_deleting_the_live_load_does_not_take_the_book_with_it(db_session):
    """The live batch owns the whole book after a carry-forward. Deleting it
    used to delete every house on the site."""
    old = _batch(db_session, batch_type="for_sale", status="published",
                 rows=0, filename="lastweek.csv")
    live = _batch(db_session, batch_type="for_sale", status="published",
                  rows=3, is_active=True, filename="thisweek.csv")
    for i in range(3):
        _listing(db_session, live, f"{i} Market Road")
    db_session.commit()

    result = _delete(db_session, live.id)

    assert result.rows_deleted == 0
    assert result.rows_kept == 3
    assert db_session.query(PropertyForSale).count() == 3
    assert result.now_active_batch_id == old.id


def test_the_only_load_cannot_be_deleted_out_from_under_live_listings(db_session):
    from fastapi import HTTPException

    only = _batch(db_session, batch_type="for_sale", status="published",
                  rows=1, is_active=True, filename="only.csv")
    _listing(db_session, only, "1 Sole Road")
    db_session.commit()

    with pytest.raises(HTTPException) as e:
        _delete(db_session, only.id)

    assert e.value.status_code == 400
    assert "still up" in e.value.detail
    assert db_session.query(PropertyForSale).count() == 1, "nothing was removed"


def test_a_load_whose_listings_are_all_gone_deletes_completely(db_session):
    _batch(db_session, batch_type="for_sale", status="published", rows=0,
           is_active=True, filename="thisweek.csv")
    old = _batch(db_session, batch_type="for_sale", status="archived", rows=2,
                 filename="lastweek.csv")
    _listing(db_session, old, "1 Sold Road", dead=True)
    _listing(db_session, old, "2 Sold Road", dead=True)
    db_session.commit()

    result = _delete(db_session, old.id)

    assert result.rows_deleted == 2
    assert result.rows_kept == 0
    assert db_session.query(PropertyForSale).count() == 0


# ---- the two steps must agree about which batch is THE batch ----------------
#
# The dead-button fault, and the worst kind: no error, nothing in the log, and
# the data sitting right there.
#
# send_to_preview picked the newest row with status "staged". The review screen
# and the publish both went through _staged_batch, which skips a pre-live batch
# that loaded no rows. Upload a file, upload it again before publishing, and the
# two disagree — preview moves the EMPTY batch while the screen goes on
# reporting the full one as staged. So the green "Go live" button never appears,
# pressing the blue one again does nothing, and there is no way to publish.
import release


def test_the_preview_step_moves_the_batch_the_publish_step_will_take(db_session):
    real = _batch(db_session, batch_type="for_sale", rows=9_857,
                  filename="forsale_week2.csv")
    empty = _batch(db_session, batch_type="for_sale", rows=0,
                   filename="forsale_week2.csv")
    db_session.commit()

    release.send_to_preview(db_session, "Auckland")

    assert db_session.get(ImportBatch, real.id).status == "preview", (
        "preview moved a different batch than the one the screen and the publish "
        "are working on — the Go live button never appears")
    assert db_session.get(ImportBatch, empty.id).status == "staged"


def test_the_screen_reports_preview_so_the_go_live_button_appears(db_session):
    """The page shows the green button only when the summary says 'preview'.
    That string is what the person actually depends on."""
    _batch(db_session, batch_type="for_sale", rows=9_857, filename="week.csv")
    _batch(db_session, batch_type="for_sale", rows=0, filename="week.csv")
    db_session.commit()

    assert release.staged_summary(db_session, "Auckland").stage == "staged"
    release.send_to_preview(db_session, "Auckland")
    assert release.staged_summary(db_session, "Auckland").stage == "preview", (
        "the review screen still reads 'staged' after the preview step, so it "
        "keeps offering the blue button and never offers the green one")


def test_a_second_press_of_preview_does_not_undo_the_first(db_session):
    """Pressing it twice is what anybody does when nothing seems to happen."""
    real = _batch(db_session, batch_type="sold", rows=16_424, filename="sold.csv")
    _batch(db_session, batch_type="sold", rows=0, filename="sold.csv")
    db_session.commit()

    release.send_to_preview(db_session, "Auckland")
    release.send_to_preview(db_session, "Auckland")

    assert db_session.get(ImportBatch, real.id).status == "preview"
    assert release.staged_summary(db_session, "Auckland").stage == "preview"


def test_all_the_way_through_with_an_empty_re_upload_in_the_way(db_session):
    sold = _batch(db_session, batch_type="sold", rows=16_424, filename="sold.csv")
    fs = _batch(db_session, batch_type="for_sale", rows=9_857, filename="week.csv")
    _batch(db_session, batch_type="sold", rows=0, filename="sold.csv")
    _batch(db_session, batch_type="for_sale", rows=0, filename="week.csv")
    db_session.commit()

    release.send_to_preview(db_session, "Auckland")
    release.publish_release(db_session, "Auckland")

    for b in (sold, fs):
        row = db_session.get(ImportBatch, b.id)
        assert row.status == "published" and row.is_active is True, (
            f"{row.filename} holds {row.rows_inserted:,} rows and did not go live")
