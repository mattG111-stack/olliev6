"""Days to sell, measured from what we watch rather than read off a file.

    "we should have how many days to sell"
    "183 sold and you cant asnwer days too sell"

The screen showed 183 sales in the suburb and a dash where the days figure
goes. The dash was honest — the sold file's days-on-market column is empty for
almost everything in it, because 89,262 of its 116,959 rows are reconstructed
out of sale-history JSON and a history entry is a price and a date. There was
no campaign behind it to measure, and no amount of care with that column
produces a number that is not in the file.

Two things are being proved here, and they are the two ways this can go wrong:

    IT MUST NOT INVENT ONE. The reconstruction used to copy the CURRENT
    campaign's days-on-market onto a sale from 1998. That is not a gap, it is a
    wrong number, and a wrong number is worse: it is believed, and it is what
    "how long do they take to sell" would have averaged.

    IT MUST NOT FORGET. The figure is the gap between the week a listing first
    appeared and the day its advertisement stopped answering. A listing carried
    forward across four weekly loads is four rows and one house, and if the
    first-seen date is re-stamped on each load then every house has been on the
    market seven days and the answer is always a week.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from days_to_sell import MIN_HOUSES, observed_days_to_sell
from ingest import ingest_for_sale, ingest_sold
from models import BatchType, ImportBatch, PropertyForSale
from release import publish_release


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


def _load(db, slugs, filename):
    ingest_for_sale(db, _for_sale(slugs), _sold(), filename, region="Auckland",
                    publish=False, fill_missing=False)
    publish_release(db, region="Auckland")


def _live_rows(db):
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    return (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == b.id)
            .order_by(PropertyForSale.slug_id).all())


@pytest.fixture()
def week_one(db_session):
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=False)
    _load(db_session, [f"h-{i}" for i in range(20)], "week1.csv")
    return db_session


# ---- the clock starts ---------------------------------------------------------
def test_a_new_listing_gets_the_day_it_first_appeared(week_one):
    rows = _live_rows(week_one)
    assert rows, "no listings loaded"
    assert all(p.first_seen_at is not None for p in rows), (
        "a listing arrived with no first-seen date, so it can never be timed")


def test_the_clock_is_not_restarted_every_week(week_one):
    """THE ONE THAT MATTERS.

    A house advertised for six weeks appears in six weekly loads. If the
    first-seen date is re-stamped each time, every house has been on the market
    since Monday and the answer to "how long do they take to sell" is seven
    days, always, for everyone. The bug would not look like a bug — it would
    look like a very fast market.
    """
    before = {p.slug_id: p.first_seen_at for p in _live_rows(week_one)}

    # The same twenty houses, still advertised, loaded again a week later.
    _load(week_one, [f"h-{i}" for i in range(20)], "week2.csv")

    after = {p.slug_id: p.first_seen_at for p in _live_rows(week_one)}
    assert set(after) == set(before)
    for slug, first in before.items():
        assert after[slug] == first, (
            f"{slug} looks brand new again after a second load — every carried "
            f"listing would read as one week old")


def test_a_house_that_appears_later_is_not_backdated(week_one):
    """The opposite mistake: carrying the date onto a house that was not there.
    A listing that first appears in week two has been on the market since week
    two, and dating it to week one adds seven days to a campaign that never
    happened."""
    first = {p.slug_id: p.first_seen_at for p in _live_rows(week_one)}
    _load(week_one, [f"h-{i}" for i in range(20)] + ["newcomer"], "week2.csv")

    rows = {p.slug_id: p for p in _live_rows(week_one)}
    assert "newcomer" in rows
    assert rows["newcomer"].first_seen_at is not None
    assert rows["newcomer"].first_seen_at >= max(first.values())


# ---- the clock stops, and the number comes out --------------------------------
def _retire(db, rows, days_each: list[int]):
    """Mark listings gone, each after its own number of days."""
    for p, days in zip(rows, days_each):
        p.link_dead_at = p.first_seen_at + timedelta(days=days)
    db.commit()


def test_it_measures_the_gap_between_appearing_and_disappearing(week_one):
    rows = _live_rows(week_one)
    _retire(week_one, rows, [10, 20, 30, 40, 50, 60, 70, 80, 90, 100])

    seen = observed_days_to_sell(week_one, "Auckland", suburb="Papakura")
    assert seen is not None, "ten completed listings and still no figure"
    assert seen.houses == 10
    assert seen.median_days == 55.0          # midpoint of 50 and 60
    assert seen.fastest == 10 and seen.slowest == 100
    assert seen.watching == 10               # the rest are still advertised


def test_a_house_carried_across_weeks_is_measured_once(week_one):
    """Four rows, one house, one campaign. Counted per row the same listing
    votes four times and drags the median toward whatever it did."""
    _load(week_one, [f"h-{i}" for i in range(20)], "week2.csv")
    _load(week_one, [f"h-{i}" for i in range(20)], "week3.csv")

    rows = _live_rows(week_one)
    _retire(week_one, rows, [30] * 10)
    # The sweep can leave a departure date on an older copy of the same house.
    old = (week_one.query(PropertyForSale)
           .filter(PropertyForSale.slug_id == rows[0].slug_id).all())
    for p in old:
        p.link_dead_at = rows[0].link_dead_at
    week_one.commit()

    seen = observed_days_to_sell(week_one, "Auckland", suburb="Papakura")
    assert seen.houses == 10, "a carried listing was counted more than once"


def test_nothing_is_reported_from_too_few_houses(week_one):
    """Four houses is four houses. Publishing their middle value as the
    suburb's days-to-sell invents a market statistic out of a coincidence."""
    rows = _live_rows(week_one)
    _retire(week_one, rows[:MIN_HOUSES - 1], [30] * (MIN_HOUSES - 1))

    assert observed_days_to_sell(week_one, "Auckland", suburb="Papakura") is None


def test_an_empty_book_is_none_and_never_zero(db_session):
    """A fabricated zero cannot be told apart from an instant market, and it
    would be believed."""
    assert observed_days_to_sell(db_session, "Auckland") is None


def test_a_listing_still_advertised_does_not_shorten_the_figure(week_one):
    """An unsold house has no time-to-sell yet. Folding today's date in would
    let listings that have NOT sold decide how fast houses sell."""
    rows = _live_rows(week_one)
    _retire(week_one, rows[:10], [30] * 10)

    seen = observed_days_to_sell(week_one, "Auckland", suburb="Papakura")
    assert seen.median_days == 30.0
    assert seen.houses == 10 and seen.watching == 10


def test_an_impossible_span_is_dropped(week_one):
    """A listing whose advertisement died the same hour it appeared is a data
    fault, not a sale in zero days, and a six-year campaign is stale data."""
    rows = _live_rows(week_one)
    _retire(week_one, rows[:12], [40] * 10 + [0, 2200])

    seen = observed_days_to_sell(week_one, "Auckland", suburb="Papakura")
    assert seen.houses == 10
    assert seen.median_days == 40.0


def test_a_staged_upload_does_not_count_its_houses_twice(week_one):
    """Mid-upload the same house sits in both the live batch and the staged
    one. Counted from both, a figure moves for no reason but the time of day.
    """
    rows = _live_rows(week_one)
    _retire(week_one, rows, [30] * 10)
    before = observed_days_to_sell(week_one, "Auckland", suburb="Papakura")

    ingest_for_sale(week_one, _for_sale([f"h-{i}" for i in range(20)]), _sold(),
                    "staged.csv", region="Auckland", publish=False,
                    fill_missing=False)          # staged, deliberately unpublished

    after = observed_days_to_sell(week_one, "Auckland", suburb="Papakura")
    assert after.houses == before.houses
    assert after.median_days == before.median_days


# ---- the tile that showed a dash ---------------------------------------------
def test_the_suburb_tile_fills_in_from_what_we_watched(week_one):
    """"183 sold and you cant asnwer days too sell". The sold rows here carry
    no listing date, exactly like the real file, so the tile has nothing to
    print until the watched listings answer instead."""
    from routers.properties import suburb_stats

    blank = suburb_stats(suburb="Papakura", region="Auckland", from_year=None,
                         to_year=None, ptype=None, db=week_one)
    assert blank.sold_count > 0, "the fixture must have sales, as the screen did"
    assert blank.median_days is None, "these sales have no listing date to read"

    _retire(week_one, _live_rows(week_one), [10, 20, 30, 40, 50, 60, 70, 80, 90, 100])

    filled = suburb_stats(suburb="Papakura", region="Auckland", from_year=None,
                         to_year=None, ptype=None, db=week_one)
    assert filled.median_days == 55.0, "the tile is still showing a dash"


def test_the_file_still_wins_when_it_has_something_to_say(week_one):
    """Portal-measured campaigns are the better answer where they exist. The
    watched figure is the fallback, not a replacement — otherwise a suburb's
    number would change meaning depending on which source happened to fill up
    first."""
    from models import PropertySold
    from routers.properties import suburb_stats

    for p in week_one.query(PropertySold).all():
        p.days_on_market = 21
    _retire(week_one, _live_rows(week_one), [90] * 12)
    week_one.commit()

    assert suburb_stats(suburb="Papakura", region="Auckland",
                        from_year=None, to_year=None, ptype=None,
                        db=week_one).median_days == 21


# ---- and the number it must never print --------------------------------------
def test_an_old_sale_does_not_inherit_todays_campaign():
    """A sale from 1998 reconstructed out of sale-history JSON carries a price
    and a date. Copying the 2026 campaign's days-on-market onto it states that
    the 1998 campaign ran that long, which is not a gap but a fabrication."""
    from ingest import _explode_sale_history

    df = pd.DataFrame([{
        "address": "1 History Lane", "suburb": "Papakura", "region": "Auckland",
        "price_numeric": 1_200_000, "sold_listing_date": "2026-08-01",
        "days_on_market": 34, "listing_date": "2026-06-28",
        "sale_method": "auction", "listing_published_date": "2026-06-28",
        "sale_history_json": (
            '[{"salePrice": 1200000, "saleDate": "2026-08-01"},'
            ' {"salePrice": 240000, "saleDate": "1998-03-14"}]'),
    }])
    out, added = _explode_sale_history(df)
    assert added >= 1, "the history entry was not turned into a sale"

    old = out[out["price_numeric"] == 240000]
    assert len(old) == 1
    row = old.iloc[0]
    for field in ("days_on_market", "listing_date", "sale_method",
                  "listing_published_date"):
        assert pd.isna(row[field]) or row[field] is None, (
            f"the 1998 sale inherited {field} from the 2026 campaign")

    # And the campaign that really happened keeps its own numbers. Blanking
    # every expanded row would have thrown away the one measurement in the
    # file to avoid inventing the others.
    card = out[out["price_numeric"] == 1_200_000]
    assert len(card) == 1
    assert card.iloc[0]["days_on_market"] == 34
    assert card.iloc[0]["sale_method"] == "auction"
