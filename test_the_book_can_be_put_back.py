"""The listings a short upload hid are still there, and come back without a file.

    "becuase the other data is still there right ?"
    "im not reup loading the data it too muck work if the data is still there
     just make it show"

Both true, with one qualifier that matters. ARCHIVING a batch only hides it, so
a book that vanished when a 672-row file went live over a 9,281-row one is
recoverable from the database alone.

But retention is real: the newest twelve batches per type and region are kept
and the rest are DELETED, rows and all. So an archived book survives about a
dozen more uploads, not for ever — which is why the restore is a button on the
screen rather than a note in a file somebody reads in a month.

This is the same rule as the weekly carry, sourced from the archive instead of
from the live batch. It copies rather than re-prices: the rows were priced when
they loaded and are sitting there complete, and re-running the pipeline over
8,000 of them to reach almost the same numbers would turn a repair into an
hour-long job that can fail halfway.

The dangerous failure here is not restoring too little, it is restoring too
much — the same house twice, or a listing whose advertisement is gone. So most
of what follows is about what must NOT come back.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from app.ingest import ingest_for_sale, ingest_sold
from app.models import BatchType, ImportBatch, PropertyForSale
from app.release import (publish_release, restorable_count,
                         restore_missing_listings)


def _sold() -> pd.DataFrame:
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
        "sale_method": "fixed price", "image_1_url": f"https://cdn.example/{s}.jpg",
        "cv_numeric": 1_000_000, "land_value_numeric": 700_000,
        "improvement_value_numeric": 300_000,
        "key_bedrooms": 3, "key_bathrooms": 1, "key_floor_area": 140,
        "key_land_area": 750, "type_of_title": "Freehold",
    } for s in slugs])


def _upload(db, slugs, filename):
    return ingest_for_sale(db, _for_sale(slugs), _sold(), filename,
                           region="Auckland", publish=False, fill_missing=False)


def _live(db):
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    if b is None:
        return b, []
    return b, (db.query(PropertyForSale)
               .filter(PropertyForSale.import_batch_id == b.id).all())


@pytest.fixture()
def after_the_fault(db_session, monkeypatch):
    """The exact state the account was left in: a big book archived underneath a
    short one. Reproduced by disabling the carry, which is what the bug did."""
    from app import ingest

    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _upload(db_session, [f"big-{i}" for i in range(40)], "week1.csv")
    publish_release(db_session, region="Auckland")

    monkeypatch.setattr(ingest, "carry_forward",
                        lambda *a, **k: pd.DataFrame())      # the fault
    _upload(db_session, ["small-0", "small-1"], "week2.csv")
    publish_release(db_session, region="Auckland")
    monkeypatch.undo()

    _, rows = _live(db_session)
    assert len(rows) == 2, "the fixture did not reproduce the fault"
    return db_session


# ---- it is still there ------------------------------------------------------
def test_the_older_batch_is_hidden_not_deleted(after_the_fault):
    """The question that had to be answered before any of the rest of this."""
    db = after_the_fault
    assert db.query(PropertyForSale).count() == 42
    archived = (db.query(ImportBatch)
                .filter(ImportBatch.status == "archived",
                        ImportBatch.batch_type == BatchType.FOR_SALE.value).all())
    assert archived, "the older batch is gone, not merely archived"


def test_the_button_can_say_how_many_before_it_is_pressed(after_the_fault):
    """Read-only. 'Restore 40 listings' beats 'press this and find out'."""
    db = after_the_fault
    n = restorable_count(db, "Auckland")
    assert n["restorable"] == 40
    assert db.query(PropertyForSale).count() == 42, "counting changed something"


# ---- it comes back ----------------------------------------------------------
def test_the_listings_come_back(after_the_fault):
    db = after_the_fault
    out = restore_missing_listings(db, "Auckland")
    assert out["restored"] == 40
    _, rows = _live(db)
    assert len(rows) == 42
    slugs = {p.slug_id for p in rows}
    assert "small-0" in slugs, "the new listings were pushed out by the old ones"
    assert "big-0" in slugs


def test_a_restored_listing_arrives_whole(after_the_fault):
    """A house that comes back without its price or its photograph has not come
    back — it has become a row nobody can use."""
    db = after_the_fault
    before = (db.query(PropertyForSale)
              .filter(PropertyForSale.slug_id == "big-7").one())
    keep = {c: getattr(before, c) for c in
            ("address", "suburb", "asking_price", "fair_value", "image_url",
             "url", "cv_numeric", "opportunity_score", "listing_type")}
    restore_missing_listings(db, "Auckland")

    _, rows = _live(db)
    after = [p for p in rows if p.slug_id == "big-7"]
    assert len(after) == 1
    for c, v in keep.items():
        assert getattr(after[0], c) == v, f"{c} was lost on the way back"


def test_the_batch_count_matches_what_is_in_it(after_the_fault):
    """The review screen reads rows_inserted. Leaving it at 2 would mean the
    listings are on the site and the dashboard says they are not."""
    db = after_the_fault
    restore_missing_listings(db, "Auckland")
    b, rows = _live(db)
    assert b.rows_inserted == len(rows) == 42


# ---- and nothing comes back that should not ---------------------------------
def test_a_house_in_both_is_not_duplicated(after_the_fault):
    """The live batch already has it. Restoring the archived copy would put the
    same house on the site twice, at two different prices."""
    db = after_the_fault
    b = (db.query(ImportBatch)
         .filter(ImportBatch.is_active.is_(True),
                 ImportBatch.batch_type == BatchType.FOR_SALE.value).one())
    dup = (db.query(PropertyForSale)
           .filter(PropertyForSale.slug_id == "big-3").one())
    db.add(PropertyForSale(import_batch_id=b.id, slug_id="big-3",
                           address=dup.address, suburb=dup.suburb,
                           is_underpriced=False, is_cashflow_positive=False,
                           is_subdividable=False))
    db.commit()

    restore_missing_listings(db, "Auckland")
    _, rows = _live(db)
    assert len([p for p in rows if p.slug_id == "big-3"]) == 1


def test_a_listing_whose_advertisement_is_gone_stays_gone(after_the_fault):
    """The link check found it dead. Restoring it would send a paying customer
    to a 404 from our own site — the exact thing that check exists to stop."""
    db = after_the_fault
    dead = (db.query(PropertyForSale)
            .filter(PropertyForSale.slug_id == "big-5").one())
    dead.link_dead_at = datetime.now(timezone.utc)
    db.commit()

    assert restorable_count(db, "Auckland")["restorable"] == 39
    restore_missing_listings(db, "Auckland")
    _, rows = _live(db)
    assert "big-5" not in {p.slug_id for p in rows}


def test_pressing_it_twice_restores_nothing_the_second_time(after_the_fault):
    """An operator who does not see it work will press it again."""
    db = after_the_fault
    assert restore_missing_listings(db, "Auckland")["restored"] == 40
    again = restore_missing_listings(db, "Auckland")
    assert again["restored"] == 0
    _, rows = _live(db)
    assert len(rows) == 42, "the second press duplicated the book"


def test_a_staged_batch_is_not_a_source(after_the_fault):
    """A batch that never went live was never part of the book. Pulling from one
    would publish rows an operator declined to publish."""
    db = after_the_fault
    _upload(db, ["never-approved-0", "never-approved-1"], "rejected.csv")
    restore_missing_listings(db, "Auckland")
    _, rows = _live(db)
    assert not [p for p in rows if (p.slug_id or "").startswith("never-approved")]


def test_it_is_quiet_when_there_is_nothing_wrong(db_session):
    """The normal case. On a healthy account this must find nothing and say so
    rather than inventing work."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _upload(db_session, ["a", "b", "c"], "week1.csv")
    publish_release(db_session, region="Auckland")
    _upload(db_session, ["d"], "week2.csv")
    publish_release(db_session, region="Auckland")

    assert restorable_count(db_session, "Auckland")["restorable"] == 0
    assert restore_missing_listings(db_session, "Auckland")["restored"] == 0
    _, rows = _live(db_session)
    assert len(rows) == 4, "the carry and the restore double-counted each other"


# ---- retention is the one place that really deletes -------------------------
def test_the_live_batch_is_never_pruned(db_session):
    """A landmine, found while checking whether the archive survives.

    Retention keeps the newest N batches by upload date and DELETES the rest,
    rows and all. "Newest" and "live" are not the same thing: a batch goes live
    when an operator presses Publish, and staged batches keep arriving in the
    meantime. Enough uploads reviewed and not published would push the batch the
    site is actually serving out of the window — every listing on the site
    deleted, for good, by a routine tidy-up at the end of an upload that changed
    nothing a customer could see.
    """
    from app import ingest

    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _upload(db_session, ["live-0", "live-1"], "live.csv")
    publish_release(db_session, region="Auckland")
    live_id, _ = _live(db_session)
    live_id = live_id.id

    # More staged uploads than the retention window, none of them published.
    for i in range(4):
        ingest.ingest_for_sale(db_session, _for_sale([f"staged-{i}"]), _sold(),
                               f"staged{i}.csv", region="Auckland",
                               publish=False, fill_missing=False)
        ingest._prune_old_batches(db_session, BatchType.FOR_SALE.value,
                                  "Auckland", keep_last=2)
        db_session.commit()

    assert db_session.get(ImportBatch, live_id) is not None, (
        "the batch the site is serving was deleted by retention")
    b, rows = _live(db_session)
    assert b.id == live_id and len(rows) == 2, "the live listings are gone"


def test_retention_still_prunes_what_it_should(db_session):
    """Pinning the live batch must not turn retention off — the table would
    grow without limit, and every carried book is a full copy of the last."""
    from app import ingest

    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    for i in range(5):
        ingest.ingest_for_sale(db_session, _for_sale([f"s-{i}"]), _sold(),
                               f"b{i}.csv", region="Auckland",
                               publish=False, fill_missing=False)
    ingest._prune_old_batches(db_session, BatchType.FOR_SALE.value,
                              "Auckland", keep_last=2)
    db_session.commit()
    left = (db_session.query(ImportBatch)
            .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value).count())
    assert left == 2, f"retention kept {left} batches instead of 2"
