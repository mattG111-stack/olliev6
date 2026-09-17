"""The delisting sweep has to actually get round the book.

    "and we have the process where it trys to open the listings once a week and
     if it cant they are deleted ?"

Nearly. It runs DAILY, and it hides rather than deletes — a dead link cannot
tell a sale from a withdrawal from a portal having a bad night, so nothing in
that module ever writes the word "sold".

But the important half of the question is whether it works, and it now carries
much more weight than it used to: since a short upload no longer replaces the
book, this sweep is the ONLY thing that takes a listing off the site. If it
cannot get round the book, the book only ever grows, and it fills up with houses
that sold months ago.

Two things stopped it getting round, both found by asking that question:

  1. It queried EVERY for-sale row in the table, and the table holds every batch
     ever loaded — the live one plus up to a dozen archived. With a fixed daily
     budget, most of a pass was spent asking about listings nobody can see.

  2. The weekly carry did not bring a listing's check history with it, so after
     every upload the whole book looked never-checked. The sweep works
     oldest-checked-first, so it restarted at the top each week and never
     reached the bottom.

Neither showed up as an error. The sweep ran, reported hundreds of checks, and
was quietly re-checking the same front slice for ever.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app import ingest
from app.ingest import ingest_for_sale, ingest_sold
from app.models import BatchType, ImportBatch, PropertyForSale
from app.portals import delisted
from app.release import publish_release


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
        "sale_method": "fixed price",
        "cv_numeric": 1_000_000, "land_value_numeric": 700_000,
        "improvement_value_numeric": 300_000,
        "key_bedrooms": 3, "key_bathrooms": 1, "key_floor_area": 140,
        "key_land_area": 750, "type_of_title": "Freehold",
    } for s in slugs])


def _upload(db, slugs, filename):
    return ingest_for_sale(db, _for_sale(slugs), _sold(), filename,
                           region="Auckland", publish=False, fill_missing=False)


def _live_row(db, slug: str) -> PropertyForSale:
    """The copy of a listing that is actually on the site."""
    live = (db.query(ImportBatch.id)
            .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                    ImportBatch.is_active.is_(True))
            .order_by(ImportBatch.id.desc()).first())
    return (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == live[0],
                    PropertyForSale.slug_id == slug).one())


class _Client:
    """A portal that answers 200 to everything, and counts who was asked."""

    def __init__(self):
        self.asked: list[str] = []

    def head(self, url):
        self.asked.append(url)

        class R:
            status_code = 200
        return R()

    def get(self, url):
        return self.head(url)

    def close(self):
        pass


# ---- the budget goes where a customer is looking ----------------------------
def test_the_sweep_only_asks_about_listings_that_are_on_the_site(db_session):
    """It used to ask about every batch ever loaded. With a dozen on file that
    is roughly one useful check in twelve, and nothing said so — the run log
    reported hundreds of checks either way."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _upload(db_session, [f"old-{i}" for i in range(6)], "week1.csv")
    publish_release(db_session, region="Auckland")
    # A second, smaller load goes live; the first is archived but still on file.
    _upload(db_session, [f"new-{i}" for i in range(2)], "week2.csv")
    publish_release(db_session, region="Auckland")

    # Counted, not compared by URL: the carry copies a listing into the new
    # batch, so the same address legitimately exists in both. The question is
    # how many CHECKS were spent, and the archived copies are the waste.
    live_ids = {b.id for b in db_session.query(ImportBatch)
                .filter(ImportBatch.is_active.is_(True)).all()}
    on_the_site = (db_session.query(PropertyForSale)
                   .filter(PropertyForSale.import_batch_id.in_(live_ids)).count())
    everything = db_session.query(PropertyForSale).count()
    assert everything > on_the_site, "the fixture has no archived rows"

    c = _Client()
    out = delisted.sweep(db_session, client=c, sleep=lambda *_: None)
    assert out["checked"] == on_the_site == len(c.asked), (
        f"{len(c.asked)} checks spent on a site showing {on_the_site} listings "
        f"— the rest went to rows nobody can see")


def test_a_staged_batch_is_not_swept_as_if_it_were_live(db_session):
    """A batch waiting for approval is not on the site, so a dead link in it
    costs nobody anything — and checking it costs a check that a live listing
    needed."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _upload(db_session, ["live-0"], "week1.csv")
    publish_release(db_session, region="Auckland")
    _upload(db_session, ["staged-0"], "week2.csv")          # not published

    c = _Client()
    delisted.sweep(db_session, client=c, sleep=lambda *_: None)
    assert not [u for u in c.asked if "staged-0" in u]


# ---- the rota survives an upload --------------------------------------------
def test_a_carried_listing_remembers_it_was_checked(db_session):
    """THE ONE THAT MATTERS. The sweep works oldest-checked-first. If every
    upload resets the whole book to never-checked, it restarts at the top each
    week and never reaches the bottom — for ever, silently."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _upload(db_session, [f"a-{i}" for i in range(4)], "week1.csv")
    publish_release(db_session, region="Auckland")

    when = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for p in db_session.query(PropertyForSale).all():
        p.link_checked_at = when
        p.link_last_result = "200"
    db_session.commit()

    _upload(db_session, ["b-0"], "week2.csv")
    publish_release(db_session, region="Auckland")

    # Asked of the LIVE batch specifically. Querying every row would find the
    # archived original, which of course still has its check history — and the
    # test would pass while the copy on the site had none.
    carried = _live_row(db_session, "a-2")
    assert carried.link_checked_at is not None, (
        "the carried listing came back as never-checked, so the sweep will "
        "start again from the top of the book after every upload")
    assert carried.link_last_result == "200"


def test_the_new_listings_are_checked_first(db_session):
    """The other half of the same rule: a listing that has never been checked
    must sort ahead of one checked last week, or new listings wait behind the
    whole book."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _upload(db_session, [f"a-{i}" for i in range(4)], "week1.csv")
    publish_release(db_session, region="Auckland")
    for p in db_session.query(PropertyForSale).all():
        p.link_checked_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    db_session.commit()

    _upload(db_session, ["brand-new"], "week2.csv")
    publish_release(db_session, region="Auckland")

    c = _Client()
    delisted.sweep(db_session, limit=1, client=c, sleep=lambda *_: None)
    assert c.asked and "brand-new" in c.asked[0]


def test_a_listing_already_counted_gone_keeps_its_count(db_session):
    """Three separate days of evidence before the advertisement is called gone.
    Resetting the streak on every upload would mean the third day never comes."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _upload(db_session, ["a-0"], "week1.csv")
    publish_release(db_session, region="Auckland")
    p = db_session.query(PropertyForSale).one()
    p.link_gone_count = 2
    db_session.commit()

    _upload(db_session, ["b-0"], "week2.csv")
    publish_release(db_session, region="Auckland")
    assert _live_row(db_session, "a-0").link_gone_count == 2


# ---- and it still answers the original question correctly -------------------
def test_it_hides_rather_than_deletes():
    """'if it cant they are deleted ?' — no. Hidden, and reversibly: a link that
    answers again puts the listing straight back. A dead link cannot tell a sale
    from a withdrawal, and deleting on that evidence is unrecoverable."""
    import inspect

    src = inspect.getsource(delisted)
    assert ".delete(" not in src, "the sweep deletes rows"
    assert "link_dead_at = None" in src or "row.link_dead_at = None" in src, (
        "nothing puts a listing back when its link answers again")


def test_only_a_404_counts_as_evidence():
    """A timeout, a 403 or a rate limit means we could not look, not that the
    listing is gone. This is the difference between one wrong answer and
    thousands at once."""
    assert delisted.GONE_CODES == {404, 410}
