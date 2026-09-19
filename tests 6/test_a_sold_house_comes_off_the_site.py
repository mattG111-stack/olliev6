"""A house that has sold should not still be for sale on our site.

    "ive just noticed alot of houses have sold in the last few weeks and we
     still have them on the site shouldnt they be gone ?"

They should, and the link check was never going to catch them. It fires on 404
and 410, and a New Zealand portal does not delete a page when a house sells — it
leaves it up with a SOLD banner, answering 200 for ever. The one signal we watch
for is the one signal these houses never send.

Meanwhile the sale arrives in the weekly sold file with an address, a price and
a date. Direct evidence, and we have had it the whole time.

THE TRAP, AND IT IS THE REASON THIS NEEDED CARE RATHER THAN A JOIN. The sold
file is a HISTORY: the 1998 sale, the 2007 sale and the 2019 sale of the same
house are all in it. Match on address alone and every listing whose property has
ever changed hands is retired — which is nearly all of them. The site empties
overnight and nothing errors.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ingest import ingest_for_sale, ingest_sold
from models import BatchType, ImportBatch, PropertyForSale
from release import publish_release
from sold_sweep import RECENT_SALE_DAYS, sweep

NOW = datetime.now(timezone.utc)


def _sold_rows(entries) -> pd.DataFrame:
    """entries: [(address, suburb, iso_date)]"""
    return pd.DataFrame([{
        "address": a, "suburb": sub, "district": sub, "region": "Auckland",
        "property_type": "House", "key_bedrooms": 3, "key_bathrooms": 1,
        "key_floor_area": "140 sqm", "key_land_area": "700 sqm",
        "cv_numeric": 1_000_000, "price_numeric": 1_100_000,
        "sale_price": 1_100_000, "land_value_numeric": 700_000,
        "improvement_value_numeric": 300_000, "type_of_title": "Freehold",
        # The loader reads the sale date from sold_listing_date — "sold_date" is
        # the column it WRITES, and feeding that name in is silently ignored.
        # Cost an hour: the sweep found no sales at all and looked broken.
        "sold_listing_date": when,
    } for a, sub, when in entries])


def _for_sale(addresses) -> pd.DataFrame:
    return pd.DataFrame([{
        "address": a, "suburb": "Papakura", "district": "Papakura",
        "region": "Auckland", "property_type": "House",
        "slug_id": f"s-{i}", "url": f"https://a-portal.example/{i}",
        "price_display": "$1,100,000", "price_numeric": 1_100_000,
        "sale_method": "fixed price", "image_1_url": f"https://cdn.example/{i}.jpg",
        "cv_numeric": 1_000_000, "land_value_numeric": 700_000,
        "improvement_value_numeric": 300_000, "key_bedrooms": 3,
        "key_bathrooms": 1, "key_floor_area": 140, "key_land_area": 700,
        "type_of_title": "Freehold",
    } for i, a in enumerate(addresses)])


def _live(db):
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    return {p.address: p for p in db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == b.id).all()}


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).date().isoformat()


@pytest.fixture()
def site(db_session):
    """Three houses on the site. One sold last week, one sold in 2019, one never."""
    ingest_sold(db_session, _sold_rows([
        ("10 Sold Street", "Papakura", _iso(7)),
        ("20 Old Street", "Papakura", "2019-05-04"),
    ]), "sold.csv", region="Auckland", publish=False)
    ingest_for_sale(db_session,
                    _for_sale(["10 Sold Street", "20 Old Street",
                               "30 Still Street"]),
                    _sold_rows([("10 Sold Street", "Papakura", _iso(7))]),
                    "week1.csv", region="Auckland", publish=False,
                    fill_missing=False)
    publish_release(db_session, region="Auckland")
    # Every listing first seen a month ago, so a week-old sale is newer than it.
    for p in _live(db_session).values():
        p.first_seen_at = NOW - timedelta(days=30)
    db_session.commit()
    return db_session


# ---- the thing that was broken -----------------------------------------------
def test_a_house_that_sold_since_we_listed_it_comes_off(site):
    got = sweep(site)
    site.commit()
    assert got["matched"] == 1, got
    assert _live(site)["10 Sold Street"].sold_seen_at is not None


def test_it_records_which_sale_it_matched_on(site):
    """An operator has to be able to check the match rather than take it on
    trust — this hides a listing a customer is paying to see."""
    sweep(site)
    site.commit()
    assert _live(site)["10 Sold Street"].sold_record_date == _iso(7)


def test_a_sold_listing_disappears_from_the_customer_list(site):
    from routers.properties import _hide_bad_data

    b = (site.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    before = _hide_bad_data(site.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == b.id)).count()
    sweep(site)
    site.commit()
    after = _hide_bad_data(site.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == b.id)).count()
    assert after == before - 1


# ---- the trap ----------------------------------------------------------------
def test_a_sale_from_years_ago_does_not_retire_a_listing(site):
    """THE ONE THAT MATTERS. The sold file is a history. Matching on address
    alone retires every listing whose house has ever changed hands, which is
    nearly all of them — the site empties overnight and nothing errors."""
    sweep(site)
    site.commit()
    assert _live(site)["20 Old Street"].sold_seen_at is None, (
        "a 2019 sale took a live listing off the site")


def test_a_house_with_no_sale_at_all_is_untouched(site):
    sweep(site)
    site.commit()
    assert _live(site)["30 Still Street"].sold_seen_at is None


def test_the_counts_say_why_each_one_was_left(site):
    """A sweep that changes nothing and a sweep with nothing to look at are
    different states, and only one of them is a problem."""
    got = sweep(site)
    assert got["checked"] == 3
    assert got["matched"] == 1
    assert got["too_old"] == 1        # the 2019 sale
    assert got["no_sale"] == 1        # never sold


def test_running_it_twice_changes_nothing_the_second_time(site):
    sweep(site); site.commit()
    again = sweep(site)
    assert again["matched"] == 0 and again["already"] == 1


# ---- the edges that decide whether it is usable ------------------------------
def test_a_sale_just_before_we_first_saw_it_still_counts(site):
    """Portals leave a listing up through the unconditional period, and the sold
    file often carries the agreement date rather than settlement. Without the
    grace window the commonest case of all — sold days after we indexed it —
    is missed on a technicality."""
    p = _live(site)["10 Sold Street"]
    p.first_seen_at = NOW - timedelta(days=5)      # sale is 7 days ago
    site.commit()
    assert sweep(site)["matched"] == 1


def test_an_old_row_with_no_first_seen_date_falls_back_to_the_calendar(site):
    """Rows loaded before that column existed are the ones most likely to be
    stale, so they must not simply be skipped."""
    for p in _live(site).values():
        p.first_seen_at = None
    site.commit()
    got = sweep(site)
    site.commit()
    assert got["matched"] == 1
    assert _live(site)["10 Sold Street"].sold_seen_at is not None
    assert _live(site)["20 Old Street"].sold_seen_at is None


def test_the_calendar_fallback_has_a_bound(site):
    """And that bound must be a real window, not "any sale ever"."""
    assert 30 <= RECENT_SALE_DAYS <= 365


def test_nothing_happens_with_no_sold_data(db_session):
    ingest_for_sale(db_session, _for_sale(["1 Only Street"]),
                    _sold_rows([("1 Only Street", "Papakura", _iso(7))]),
                    "week1.csv", region="Auckland", publish=False,
                    fill_missing=False)
    publish_release(db_session, region="Auckland")
    db_session.query(__import__("models", fromlist=["PropertySold"])
                     .PropertySold).delete()
    db_session.commit()
    assert sweep(db_session)["matched"] == 0


def test_it_never_deletes_anything(site):
    before = site.query(PropertyForSale).count()
    sweep(site)
    site.commit()
    assert site.query(PropertyForSale).count() == before
