"""A short file must not shrink the book — on the path uploads actually take.

    "so i just added the lastest data and its hidden the old data instead of
     just adding in the new ?"

    Older batch  #55 · 6/09/2026 · 9,281 rows
    Newer batch  #56 · 8/09/2026 ·   672 rows

Carrying the book forward was built, and it worked, and there is a whole file of
tests proving it works — every one of which calls `ingest.carry_forward()`
DIRECTLY. Not one of them went through an upload. So when the audit screen was
added and every real upload started arriving as `publish=False`, the call site
sat behind `if publish:` and became dead code on the only path anybody uses.

Nothing looked wrong at any point. The upload succeeded. The review screen
showed exactly the rows the file contained, which is what a review screen is
supposed to do. The loss only happened at Publish, in a different module, when
the batch holding the other 8,609 listings was archived — and by then the number
on the screen had been 672 for so long it looked like the answer.

That is the recurring shape: ONE RULE, N CALLERS. The rule was right and the
tests were real; the caller was not covered. So these tests do not call
carry_forward at all. They upload a file the way the app uploads a file, press
Publish the way the operator presses Publish, and count what a customer can see.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.ingest import ingest_for_sale, ingest_sold
from app.models import BatchType, ImportBatch, PropertyForSale
from app.release import publish_release


def _sold() -> pd.DataFrame:
    """Enough comparable sales that pricing has something to work with."""
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Papakura",
        "district": "Papakura", "region": "Auckland", "property_type": "House",
        "key_bedrooms": 3, "key_bathrooms": 1,
        "key_floor_area": f"{130 + i} sqm", "key_land_area": f"{700 + i * 10} sqm",
        "cv_numeric": 1_000_000, "price_numeric": 1_050_000 + i * 10_000,
        "sale_price": 1_050_000 + i * 10_000,
        "land_value_numeric": 700_000, "improvement_value_numeric": 300_000,
        "type_of_title": "Freehold", "sold_date": "2026-08-01",
    } for i in range(12)])


def _for_sale(slugs) -> pd.DataFrame:
    return pd.DataFrame([{
        "address": f"{s} Example Road", "suburb": "Papakura",
        "district": "Papakura", "region": "Auckland", "property_type": "House",
        "slug_id": s, "url": f"https://a-portal.example/listing/{s}",
        "price_display": "$1,100,000", "price_numeric": 1_100_000,
        "sale_method": "fixed price",
        "cv_numeric": 1_000_000, "land_value_numeric": 700_000,
        "improvement_value_numeric": 300_000,
        "key_bedrooms": 3, "key_bathrooms": 1, "key_floor_area": 140,
        "key_land_area": 750, "type_of_title": "Freehold",
    } for s in slugs])


def _upload(db, slugs, filename):
    """One upload, exactly as app/routers/admin_upload.py performs it — which
    is to say STAGED. There is no other way in; every real upload since the
    audit screen landed has come through here with publish=False."""
    return ingest_for_sale(db, _for_sale(slugs), _sold(), filename,
                           region="Auckland", publish=False, fill_missing=False)


def _live_slugs(db) -> set[str]:
    """What a customer can actually see."""
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    if b is None:
        return set()
    return {p.slug_id for p in db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == b.id).all()}


def _staged_slugs(db) -> set[str]:
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.status == "staged")
         .order_by(ImportBatch.id.desc()).first())
    if b is None:
        return set()
    return {p.slug_id for p in db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == b.id).all()}


@pytest.fixture()
def week_one(db_session):
    """A full book, live — the state the account was in before the short file."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _upload(db_session, [f"big-{i}" for i in range(40)], "week1.csv")
    publish_release(db_session, region="Auckland")
    assert len(_live_slugs(db_session)) == 40
    return db_session


# ---- the reported fault -----------------------------------------------------
def test_a_short_upload_does_not_hide_the_book(week_one):
    """THE ONE THAT MATTERS. 672 rows arriving over 9,281 left 672."""
    db = week_one
    _upload(db, ["small-0", "small-1"], "week2.csv")
    publish_release(db, region="Auckland")

    live = _live_slugs(db)
    assert "small-0" in live and "small-1" in live, "the new listings are missing"
    assert len(live) == 42, (
        f"the book went from 40 listings to {len(live)}. A short file is a "
        f"scrape that caught less, not a market that emptied overnight")


def test_the_operator_audits_the_whole_book_not_just_the_new_rows(week_one):
    """The review screen is where this was survivable and was not caught: it
    showed 672 rows and was RIGHT to, because 672 rows were all the batch had.
    The carry has to happen at load, so what is reviewed is what goes live."""
    db = week_one
    _upload(db, ["small-0"], "week2.csv")
    assert len(_staged_slugs(db)) == 41, (
        "the batch waiting for approval is not the batch that will go live")


def test_nothing_is_live_until_publish_is_pressed(week_one):
    """The carry must not quietly promote the staged batch. Until somebody
    approves it the site keeps serving the batch it was serving."""
    db = week_one
    _upload(db, ["small-0"], "week2.csv")
    assert "small-0" not in _live_slugs(db)
    assert len(_live_slugs(db)) == 40


def test_the_newest_file_wins_where_the_two_overlap(week_one):
    """Carrying must not duplicate a house that IS in the new file."""
    db = week_one
    _upload(db, ["big-0", "big-1", "small-0"], "week2.csv")
    publish_release(db, region="Auckland")

    live = _live_slugs(db)
    assert len(live) == 41, "a listing in both files was carried as well as loaded"
    rows = [p for p in db.query(PropertyForSale).all() if p.slug_id == "big-0"]
    live_ids = {b.id for b in db.query(ImportBatch)
                .filter(ImportBatch.is_active.is_(True)).all()}
    assert len([r for r in rows if r.import_batch_id in live_ids]) == 1


def test_a_delisted_house_still_leaves(week_one):
    """The carry is not "keep everything for ever". A listing leaves when its
    ADVERTISEMENT is gone, which the daily link check decides by opening it —
    that signal has to keep working or the book only ever grows."""
    from datetime import datetime, timezone

    db = week_one
    gone = (db.query(PropertyForSale)
            .filter(PropertyForSale.slug_id == "big-0").one())
    gone.link_dead_at = datetime.now(timezone.utc)
    db.commit()

    _upload(db, ["small-0"], "week2.csv")
    publish_release(db, region="Auckland")
    assert "big-0" not in _live_slugs(db)


def test_two_short_uploads_in_a_row_still_hold_the_book(week_one):
    """The second upload carries from the batch the first one created, so the
    loss cannot arrive one week later instead."""
    db = week_one
    _upload(db, ["small-0"], "week2.csv")
    publish_release(db, region="Auckland")
    _upload(db, ["small-1"], "week3.csv")
    publish_release(db, region="Auckland")
    assert len(_live_slugs(db)) == 42


# ---- the shape of the bug, not just this instance ---------------------------
def test_the_carry_is_not_gated_on_going_straight_live():
    """The regression in one line of source.

    `carry_forward` sat inside `if publish:`. Every real upload is publish=False,
    so the rule was correct, tested, and never executed. A test that reads the
    source is a blunt instrument, but this is a gate that was invisible from the
    outside for two builds and cost the whole book when it fired.
    """
    import inspect

    from app import ingest

    src = inspect.getsource(ingest.ingest_for_sale)
    call = src.index("carry_forward(db, region, for_sale_df)")
    before = src[:call]
    # The nearest enclosing block must not be a publish check.
    tail = before.rsplit("\n", 6)[-6:]
    assert not any(line.strip() == "if publish:" for line in tail), (
        "carrying the book forward is gated on the batch going straight live "
        "again — which is the path no real upload takes")
