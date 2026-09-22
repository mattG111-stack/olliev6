"""How many houses can a customer see, and what has been taken off.

    "in the dashbaord i need the numbers of what is live and what has been
     deleted via our sold check"

    "and to be able to download all live data in csv"

Everything the review screen showed described the batch being REVIEWED. None of
it answered the question an operator opens that page to ask. The week the book
fell from 9,281 listings to 657 the figure was in the database the whole time
and on no screen at all — the fault was found by a person scrolling a property
list and counting, days later.

THE RULE THESE TESTS EXIST FOR: the dashboard's "live" number and the site's own
list must be the same number. A dashboard that disagrees with the page it
describes is worse than no dashboard, because it is believed. So the count is
not a restatement of the visibility rule — it reuses the site's own filter, and
the test below proves the two agree by asking each of them separately.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pandas as pd
import pytest

from ingest import ingest_for_sale, ingest_sold
from models import BatchType, ImportBatch, PropertyForSale
from release import publish_release, staged_summary


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


@pytest.fixture()
def live(db_session):
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    ingest_for_sale(db_session, _for_sale([f"h-{i}" for i in range(30)]), _sold(),
                    "week1.csv", region="Auckland", publish=False,
                    fill_missing=False)
    publish_release(db_session, region="Auckland")
    return db_session


def _rows(db):
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    return (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == b.id).all())


# ---- the number is there at all ---------------------------------------------
def test_the_dashboard_reports_what_is_live(live):
    s = staged_summary(live, "Auckland")
    assert s.live_total == 30
    assert s.live_visible == 30
    assert s.live_gone == 0


def test_it_is_there_when_nothing_is_staged(live):
    """The state the account is in most of the time, and the state it was in
    when the book collapsed. A number that only appears mid-upload is missing
    exactly when it is needed."""
    s = staged_summary(live, "Auckland")
    assert s.has_staged is False
    assert s.live_total == 30, "the live count vanished because nothing is staged"


# ---- and it is the SAME number the site shows -------------------------------
def test_the_dashboard_and_the_site_agree(live):
    """THE ONE THAT MATTERS. Asked of each side separately: the dashboard's
    count, and the site's own filter. If these ever differ, one of them is
    lying and there is no way to tell which from the screen."""
    from routers.properties import _hide_bad_data

    for p in _rows(live)[:4]:
        p.is_held = True
        p.hold_reason = "Below $10,000 margin"
    for p in _rows(live)[10:13]:
        p.link_dead_at = datetime.now(timezone.utc)
    live.commit()

    b = (live.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    on_the_site = _hide_bad_data(
        live.query(PropertyForSale)
        .filter(PropertyForSale.import_batch_id == b.id)).count()

    s = staged_summary(live, "Auckland")
    assert s.live_visible == on_the_site, (
        f"dashboard says {s.live_visible} live, the site shows {on_the_site}")


def test_the_sold_check_number_is_the_sold_check_number(live):
    """"what has been deleted via our sold check" — listings the daily link
    check opened and found gone. Not held rows, not bad data: those are their
    own counts, because they need different responses."""
    for p in _rows(live)[:5]:
        p.link_dead_at = datetime.now(timezone.utc)
    for p in _rows(live)[20:22]:
        p.is_held = True
    live.commit()

    s = staged_summary(live, "Auckland")
    assert s.live_gone == 5
    assert s.live_held == 2


def test_the_parts_add_up_to_the_whole(live):
    """A set of counts that does not reconcile is a set of counts nobody can
    act on — the first question anybody asks is where the rest went."""
    for p in _rows(live)[:3]:
        p.link_dead_at = datetime.now(timezone.utc)
    for p in _rows(live)[10:14]:
        p.is_held = True
    live.commit()

    s = staged_summary(live, "Auckland")
    assert (s.live_visible + s.live_gone + s.live_held
            + s.live_other_hidden) == s.live_total


def test_an_empty_account_reports_zero_not_a_crash(db_session):
    s = staged_summary(db_session, "Auckland")
    assert s.live_total == 0 and s.live_visible == 0


# ---- the CSV -----------------------------------------------------------------
def test_the_csv_and_the_workbook_describe_the_same_thing(live):
    """They used to keep separate column lists, so the two exports of one batch
    carried different fields under different headings and adding a column to one
    left the other behind. One list now, and this fails if that is undone."""
    from audit_export import COLUMNS
    from routers.properties import export_for_sale_csv

    src = __import__("inspect").getsource(export_for_sale_csv)
    assert "COLUMNS" in src, "the CSV has grown its own column list again"
    assert [h for h, _ in COLUMNS][:2] == ["Visible on site", "Why not visible"]


def test_the_csv_says_which_rows_a_customer_cannot_see(live):
    """The whole reason the hidden rows are in the file: a fault that only
    affects held rows is invisible on the site by definition."""
    from audit_export import COLUMNS, _visible_reason

    rows = _rows(live)
    rows[0].is_held = True
    rows[0].hold_reason = "Below $10,000 margin"
    rows[1].link_dead_at = datetime.now(timezone.utc)
    live.commit()

    assert _visible_reason(rows[0]).startswith("Held at review")
    assert "Advertisement is gone" in _visible_reason(rows[1])
    assert _visible_reason(rows[5]) is None
    # And the columns that carry it are the first two, so nobody has to hunt.
    assert [h for h, _ in COLUMNS][:2] == ["Visible on site", "Why not visible"]


# ---- week by week ------------------------------------------------------------
def test_it_shows_what_came_off_each_week(live):
    """"we want to show what is deleted weekly".

    One number says the site is smaller. It does not say whether that is a
    normal week's churn or something breaking — and those need opposite
    responses. The weeks either side are what make either legible.
    """
    from datetime import timedelta

    rows = _rows(live)
    base = datetime(2026, 9, 9, tzinfo=timezone.utc)      # a Wednesday
    for p in rows[:3]:
        p.link_dead_at = base                              # this week
    for p in rows[10:17]:
        p.link_dead_at = base - timedelta(days=7)          # last week
    live.commit()

    weeks = staged_summary(live, "Auckland").gone_weekly
    assert len(weeks) == 2
    assert weeks[0]["week_starting"] == "2026-09-07"       # Monday of that week
    assert weeks[0]["taken_off"] == 3
    assert weeks[1]["week_starting"] == "2026-08-31"
    assert weeks[1]["taken_off"] == 7


def test_a_house_carried_across_loads_is_one_departure(live):
    """A listing carried forward for a month is four rows and one house. Counted
    per row, a quiet week reads as a spike purely because the book is older."""
    from datetime import timedelta

    b = (live.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    gone_at = datetime(2026, 9, 9, tzinfo=timezone.utc)
    p = _rows(live)[0]
    p.link_dead_at = gone_at
    # The same house, from an earlier load, retired at the same moment.
    old = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                      filename="older.csv", is_active=False, status="archived")
    live.add(old); live.flush()
    live.add(PropertyForSale(import_batch_id=old.id, slug_id=p.slug_id,
                             address=p.address, suburb=p.suburb,
                             link_dead_at=gone_at, is_underpriced=False,
                             is_cashflow_positive=False, is_subdividable=False))
    live.commit()

    weeks = staged_summary(live, "Auckland").gone_weekly
    assert weeks[0]["taken_off"] == 1, "the same house was counted twice"


def test_a_quiet_week_is_simply_absent_not_a_zero(live):
    """No departures is no row. A fabricated zero for a week nothing happened
    is a number nobody can tell from a week the check did not run."""
    assert staged_summary(live, "Auckland").gone_weekly == []
