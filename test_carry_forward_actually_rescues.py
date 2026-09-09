"""Does the carry-forward actually put a price back on a listing?

    "for two days you have told me you have fixed that"

Fair. A bug was found in it and fixed, and it was reported as fixed, and nothing
ever checked the thing end to end: load a batch with prices, load another where
the same houses have gone to by-negotiation, and see whether a price comes back.
Every test up to here checked a piece — does derived_asking round correctly,
does needs_a_carried_price pick the right rows — and a chain of correct pieces
is not a working chain.

So this runs the whole thing through the database: two real loads, then the
price stage's own carry-forward, then the pricing run, then read the row.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.ingest import ingest_for_sale
from app.models import ImportBatch, PropertyForSale
from app.prior_price import (ADVERTISED_BASIS, DERIVED_BASIS,
                             carry_forward_prices)


def _sold_frame() -> pd.DataFrame:
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Epsom", "district": "Epsom",
        "region": "Auckland", "property_type": "House",
        "key_bedrooms": 4, "key_bathrooms": 2,
        "key_floor_area": f"{200 + i} sqm", "key_land_area": f"{700 + i} sqm",
        "cv_numeric": 3_200_000, "price_numeric": 3_300_000 + i * 10_000,
        "sale_price": 3_300_000 + i * 10_000, "land_value_numeric": 2_000_000,
        "improvement_value_numeric": 1_200_000, "type_of_title": "Freehold",
        "sold_date": "2026-06-01",
    } for i in range(12)])


def _listing(price, cv=3_275_000) -> dict:
    return {
        "address": "150 The Drive", "suburb": "Epsom", "district": "Epsom",
        "region": "Auckland", "property_type": "House", "slug_id": "150-the-drive",
        "cv_numeric": cv, "price_numeric": price,
        "key_floor_area": 210, "key_land_area": 720, "key_bedrooms": 4,
        "key_bathrooms": 2, "type_of_title": "Freehold",
        "land_value_numeric": 2_000_000, "improvement_value_numeric": 1_275_000,
    }


def _load_sold(db):
    """The sold history has to be IN the database, not just handed to the
    for-sale load as a frame. reprice_batch reads it back out and returns
    "no sold batch to price against" if it is not there — which is exactly the
    mistake this test made on its first run, and it looked identical to the bug
    it was written to find."""
    from app.ingest import ingest_sold

    return ingest_sold(db, _sold_frame(), "sold.csv", region="Auckland",
                       publish=True)


def _load(db, price, cv=3_275_000, filename="week.csv"):
    res = ingest_for_sale(db, pd.DataFrame([_listing(price, cv)]), _sold_frame(),
                          filename, region="Auckland", publish=True)
    return res


def _row(db) -> PropertyForSale:
    batch = (db.query(ImportBatch)
             .filter(ImportBatch.batch_type == "for_sale")
             .order_by(ImportBatch.id.desc()).first())
    return (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch.id).one()), batch


def _reread(db, batch_id) -> PropertyForSale:
    """Read the row back from the database rather than refreshing the object.

    reprice_batch calls expunge_all() to drop each chunk, so the instance held
    from before it ran is detached afterwards and refresh() raises. This is the
    second way this test managed to look like a broken carry-forward while
    testing nothing — worth the helper."""
    return (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch_id).one())


# ---- the whole chain --------------------------------------------------------
def test_a_price_from_last_week_comes_back_this_week(db_session):
    """The claim, end to end and through the database.

    Week one: on the market at $2,580,000. Week two: the vendor has withdrawn the
    price and the scraper has filled the field with the council valuation. The
    listing has to come out of week two carrying $2,580,000 less the negotiation
    discount, rounded — $2,500,000 — and saying that is where it came from.
    """
    _load(db_session, 2_580_000, filename="week1.csv")
    _load(db_session, 3_275_000, filename="week2.csv")     # asking == CV

    row, batch = _row(db_session)
    filled = carry_forward_prices(db_session, batch.id, "Auckland")

    assert filled == 1, "the carry-forward found nothing to rescue"
    db_session.refresh(row)
    assert row.prior_asking_price == 2_580_000.0
    assert row.prior_asking_seen_at is not None


def test_the_pricing_run_then_uses_it_as_the_asking_price(db_session):
    """Recording the old price is half of it. The listing has to be PRICED from
    it, or the rescue is a note in a column nobody acts on."""
    from app.reprice import reprice_batch

    _load_sold(db_session)
    _load(db_session, 2_580_000, filename="week1.csv")
    _load(db_session, 3_275_000, filename="week2.csv")
    row, batch = _row(db_session)
    # Held as an int: expunge_all() inside reprice_batch detaches the batch
    # object too, so reading batch.id afterwards raises on a detached instance.
    bid = batch.id

    carry_forward_prices(db_session, bid, "Auckland")
    res = reprice_batch(db_session, bid, region="Auckland", commit=True)
    # Checked, not assumed. reprice_batch REPORTS its failures rather than
    # raising them, so an unchecked call that did nothing at all reads exactly
    # like a carry-forward that was ignored.
    assert not res.error, res.error
    row = _reread(db_session, bid)

    assert row.asking_basis == DERIVED_BASIS, (
        f"priced as {row.asking_basis!r}, not from the carried price")
    # 2,580,000 less 3%, to the nearest $10,000.
    assert row.asking_price == 2_500_000.0


# ---- the counterweights -----------------------------------------------------
def test_a_house_with_no_history_cannot_be_rescued(db_session):
    """And this is why the real file shows none of them.

    A listing can only be given last week's price if last week's row still
    exists. Every week the region check threw the file away, the history the
    carry-forward needs was thrown away with it — so a house arriving for the
    first time has nothing behind it, however long it has really been listed.
    """
    _load(db_session, 3_275_000, filename="week1.csv")     # placeholder from day one

    row, batch = _row(db_session)
    assert carry_forward_prices(db_session, batch.id, "Auckland") == 0
    db_session.refresh(row)
    assert row.prior_asking_price is None


def test_a_placeholder_is_not_carried_forward_as_though_it_were_a_price(db_session):
    """Moving a guess between two loads would launder it into a fact."""
    _load(db_session, 3_275_000, filename="week1.csv")     # CV in the price field
    _load(db_session, 3_275_000, filename="week2.csv")

    row, batch = _row(db_session)
    assert carry_forward_prices(db_session, batch.id, "Auckland") == 0


def test_a_listing_that_still_has_a_real_price_is_left_alone(db_session):
    """The vendor's own number always wins."""
    from app.reprice import reprice_batch

    _load_sold(db_session)
    _load(db_session, 2_580_000, filename="week1.csv")
    _load(db_session, 2_490_000, filename="week2.csv")     # re-priced, still real
    row, batch = _row(db_session)
    # Held as an int: expunge_all() inside reprice_batch detaches the batch
    # object too, so reading batch.id afterwards raises on a detached instance.
    bid = batch.id

    carry_forward_prices(db_session, bid, "Auckland")
    res = reprice_batch(db_session, bid, region="Auckland", commit=True)
    assert not res.error, res.error
    row = _reread(db_session, bid)

    assert row.asking_price == 2_490_000.0
    assert row.asking_basis == ADVERTISED_BASIS


def test_running_it_twice_changes_nothing(db_session):
    """The price stage is re-runnable, so this is too."""
    _load(db_session, 2_580_000, filename="week1.csv")
    _load(db_session, 3_275_000, filename="week2.csv")
    row, batch = _row(db_session)

    carry_forward_prices(db_session, batch.id, "Auckland")
    db_session.refresh(row)
    first = (row.prior_asking_price, row.prior_asking_seen_at)

    carry_forward_prices(db_session, batch.id, "Auckland")
    db_session.refresh(row)
    assert (row.prior_asking_price, row.prior_asking_seen_at) == first
