"""Sold history accumulates; it is never replaced.

A sold batch is not a snapshot. Last month's sales are still true this month,
so loading a new sold file must ADD to what is on file rather than supersede it.
Four separate mechanisms had to agree for that to hold, and each one of them
defaulted the other way:

  * ingest archived the prior active batch on every load
  * retention pruning deleted batches beyond the last N
  * the comp loader read only the single newest batch
  * publish_release archived the previous live batch

The last one is the reason this file exists. Uploading appeared to work — the
row counts went up — and then publishing silently dropped the older batch back
out of comp matching, because the loader reads staged + published only. Nothing
errored, and the only visible symptom would have been valuations quietly
drifting as history vanished.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app import ingest
from app.models import BatchType, ImportBatch, PropertySold
from app.release import publish_release
from app.routers.admin_upload import _load_active_sold_df

REGION = "Auckland"


def _sold_row(slug: str, date: str, price: float, suburb: str = "Remuera") -> dict:
    """One sold row in the canonical layout."""
    return {
        "slug_id": slug,
        "address": f"{slug} Street",
        "suburb": suburb,
        "district": "Auckland City",
        "price_numeric": price,
        "sold_listing_date": date,
        "key_bedrooms": 3,
        "key_bathrooms": 1,
        "key_floor_area": 120,
        "key_land_area": 500,
    }


def _visible_to_comps(db) -> int:
    df = _load_active_sold_df(db, REGION)
    return 0 if df is None else len(df)


def test_a_second_load_adds_to_the_first(db_session):
    db = db_session
    ingest.ingest_sold(db, pd.DataFrame([
        _sold_row("a", "2026-01-10", 900_000),
        _sold_row("b", "2026-02-11", 950_000),
    ]), "first.csv", region=REGION, publish=False)
    assert db.query(PropertySold).count() == 2

    ingest.ingest_sold(db, pd.DataFrame([
        _sold_row("c", "2026-03-12", 1_000_000),
    ]), "second.csv", region=REGION, publish=False)
    assert db.query(PropertySold).count() == 3, "the second load replaced the first"


def test_publishing_does_not_drop_earlier_sold_batches(db_session):
    """The one that actually broke: append survived upload but not publish."""
    db = db_session
    ingest.ingest_sold(db, pd.DataFrame([_sold_row("a", "2026-01-10", 900_000)]),
                       "first.csv", region=REGION, publish=False)
    publish_release(db, REGION)
    assert _visible_to_comps(db) == 1

    ingest.ingest_sold(db, pd.DataFrame([_sold_row("b", "2026-02-11", 950_000)]),
                       "second.csv", region=REGION, publish=False)
    publish_release(db, REGION)

    assert _visible_to_comps(db) == 2, (
        "publishing the new sold batch archived the older one — comp matching "
        "can no longer see the earlier sales"
    )
    archived = (db.query(ImportBatch)
                  .filter(ImportBatch.batch_type == BatchType.SOLD.value,
                          ImportBatch.status == "archived").count())
    assert archived == 0, "a sold batch was archived; sold history must accumulate"


def test_reloading_the_same_file_inserts_nothing(db_session):
    """Re-uploading must not double every comp in the overlap."""
    db = db_session
    rows = pd.DataFrame([
        _sold_row("a", "2026-01-10", 900_000),
        _sold_row("b", "2026-02-11", 950_000),
    ])
    ingest.ingest_sold(db, rows, "file.csv", region=REGION, publish=False)
    again = ingest.ingest_sold(db, rows, "file.csv", region=REGION, publish=False)

    assert again.rows_inserted == 0
    assert db.query(PropertySold).count() == 2


def test_the_same_house_selling_twice_is_two_sales(db_session):
    """Keying dedupe on the property alone would collapse these into one."""
    db = db_session
    ingest.ingest_sold(db, pd.DataFrame([
        _sold_row("a", "2020-05-01", 600_000),
        _sold_row("a", "2026-05-01", 1_200_000),
    ]), "repeat.csv", region=REGION, publish=False)

    sales = db.query(PropertySold).filter(PropertySold.slug_id == "a").all()
    assert len(sales) == 2, "a repeat sale was discarded as a duplicate property"
    assert {s.sale_price for s in sales} == {600_000, 1_200_000}


def test_duplicates_already_stored_are_deleted(db_session):
    """Skipping at insert stops NEW duplicates; it does not clean up old ones.

    Every sold load before append mode replaced the previous batch instead of
    merging, so any overlap between files is sitting in the database as real
    duplicate rows — and a duplicated sale is a duplicated comp.
    """
    db = db_session
    row = pd.DataFrame([_sold_row("a", "2026-01-10", 900_000)])
    # Three loads the old way: nothing was skipped, so three copies of one sale.
    for i in range(3):
        ingest.ingest_sold(db, row, f"old{i}.csv", region=REGION,
                           publish=False, append=False)
    assert db.query(PropertySold).count() == 3

    ingest.ingest_sold(db, row, "new.csv", region=REGION, publish=False)
    assert db.query(PropertySold).count() == 1, "stored duplicates were not removed"


def test_the_purge_does_not_eat_repeat_sales(db_session):
    """Same house, two dates, is two sales — not a duplicate to be cleaned up."""
    db = db_session
    ingest.ingest_sold(db, pd.DataFrame([
        _sold_row("a", "2020-05-01", 600_000),
        _sold_row("a", "2026-05-01", 1_200_000),
    ]), "repeat.csv", region=REGION, publish=False)
    # A second load triggers the purge over the top of them.
    ingest.ingest_sold(db, pd.DataFrame([_sold_row("b", "2026-06-01", 800_000)]),
                       "other.csv", region=REGION, publish=False)

    sales = db.query(PropertySold).filter(PropertySold.slug_id == "a").all()
    assert {s.sale_price for s in sales} == {600_000, 1_200_000}, (
        "the duplicate purge removed a genuine repeat sale"
    )


def test_one_sale_spelled_several_ways_is_still_one_sale(db_session):
    """The same sale arrives with different date formatting per file type.

    An Excel column gives '2026-03-18 00:00:00', a CSV gives '2026-03-18', and a
    formatted column gives '18/03/2026'. Compared as raw strings those are three
    sales, so one sale gets stored three times and counted three times as a comp.
    """
    db = db_session
    for date in (pd.Timestamp("2026-03-18"), "2026-03-18", "18/03/2026",
                 "2026-03-18 00:00:00"):
        row = _sold_row("a", "unused", 900_000)
        row["sold_listing_date"] = date
        ingest.ingest_sold(db, pd.DataFrame([row]), "f.csv", region=REGION, publish=False)

    stored = db.query(PropertySold).all()
    assert len(stored) == 1, f"one sale stored {len(stored)} times across date formats"
    assert stored[0].sold_date == "2026-03-18"


def test_normalising_dates_does_not_merge_different_sales(db_session):
    """Older sales of the same house survive the canonicalisation."""
    db = db_session
    for date, price in (("2026-03-18", 900_000), ("2020-05-01", 600_000),
                        ("01/05/2012", 350_000)):
        row = _sold_row("a", "unused", price)
        row["sold_listing_date"] = date
        ingest.ingest_sold(db, pd.DataFrame([row]), "f.csv", region=REGION, publish=False)

    sales = db.query(PropertySold).filter(PropertySold.slug_id == "a").all()
    assert {s.sold_date for s in sales} == {"2026-03-18", "2020-05-01", "2012-05-01"}
    assert {s.sale_price for s in sales} == {900_000, 600_000, 350_000}


@pytest.mark.parametrize("raw,expected", [
    ("2026-03-18 00:00:00", "2026-03-18"),
    ("18/03/2026", "2026-03-18"),   # NZ is day-first
    ("2026-3-8", "2026-03-08"),
    ("1994-02-01", "1994-02-01"),
    ("not a date", "not a date"),   # kept, not discarded
    (None, None),
])
def test_canonical_sale_date(raw, expected):
    assert ingest.canonical_sale_date(raw) == expected


def test_sale_history_json_becomes_one_row_per_sale(db_session):
    """The export carries every sale in JSON and only the latest in its columns."""
    db = db_session
    row = _sold_row("a", "2026-03-18", 820_000)
    row["sale_history_json"] = (
        '[{"saleDate": "2026-03-18", "salePrice": 820000},'
        ' {"saleDate": "2019-04-02", "salePrice": 500000},'
        ' {"saleDate": "2011-08-19", "salePrice": 300000}]'
    )
    ingest.ingest_sold(db, pd.DataFrame([row]), "history.csv", region=REGION, publish=False)

    prices = {s.sale_price for s in db.query(PropertySold).all()}
    assert prices == {820_000, 500_000, 300_000}, (
        "sale_history_json was not expanded — only the latest sale was kept"
    )
