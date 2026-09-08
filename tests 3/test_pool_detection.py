"""Never offer a pool to a house that already has one.

The property page carries a line reading "houses here with a pool sell for
+X%". On a house that already has a pool that is not advice, it is nonsense on
the screen of the person who owns it — and it was reachable, because the only
thing suppressing it was the export's own flag, which is frequently blank. A
listing that spends a paragraph on its swimming pool has told us anyway.

So the flag is settled once, at ingest, from three sources: the export's flag,
its keyword flag, and the listing text. It is also re-checked when the page is
built, so properties loaded before this existed are covered too.

The text side is deliberately conservative. A false positive silently moves a
house onto a different set of comparable sales, so anything ambiguous is left
alone — starting with "spa pool", which in New Zealand is a hot tub and appears
in thousands of listings.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold
from app.pricing.pool import detect_pool, text_says_pool
from app.routers.properties import property_value_add


# ---- what counts as a pool --------------------------------------------------
@pytest.mark.parametrize("text", [
    "Sparkling in-ground swimming pool with a large deck",
    "The heated lap pool runs the length of the section",
    "Entertain by the saltwater pool all summer",
    "Fully fenced pool, safety compliant",
    "Plunge pool and outdoor shower",
    "POOL AND SPA, north facing",
])
def test_a_listing_that_describes_a_pool_has_one(text):
    assert text_says_pool(text), text


@pytest.mark.parametrize("text", [
    # A spa pool is a hot tub. This is the common false positive in NZ listings.
    "Covered deck with a spa pool",
    "Spa-pool included in the sale",
    "Games room with a pool table",
    "Easy car pool to the city",
    "Pools of natural light throughout the living areas",
    "Walk to the public pool and library",
    "Minutes from the community pool",
    "Shared pool in the body corporate complex",
    "No pool, but room for one",
    "The section is flat — without a pool at present",
    "Close to the swimming pool complex and gym",
])
def test_things_that_are_not_a_pool_on_this_property(text):
    assert not text_says_pool(text), text


def test_nothing_said_is_not_a_pool():
    assert not text_says_pool(None, "", "   ", float("nan") and None)


# ---- the flag, from every source -------------------------------------------
def test_the_exports_own_flag_still_wins_first():
    assert detect_pool({"has_swimming_pool": True})
    assert detect_pool({"has_swimming_pool": "true"})


def test_the_keyword_flag_counts_too():
    """kw_pool is the export's own keyword hit — it was being ignored entirely."""
    assert detect_pool({"has_swimming_pool": None, "kw_pool": 1})


def test_the_text_is_read_when_the_flag_is_blank():
    """A blank flag means "not recorded", not "no"."""
    assert detect_pool({
        "has_swimming_pool": None,
        "description": "Set back from the road, with a heated swimming pool.",
    })


def test_a_blank_row_is_not_a_pool():
    assert not detect_pool({"has_swimming_pool": None, "description": None})


def test_it_reads_a_stored_property_as_well_as_an_import_row():
    """Needed at read time too — see property_value_add."""
    p = PropertyForSale(address="1 Test St", description="Large swimming pool.")
    assert detect_pool(p)


# ---- the consequence --------------------------------------------------------
def _world(db, *, description=None, flag=None):
    sold_batch = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                             filename="sold.csv", rows_total=0, is_active=True,
                             status="published")
    live = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                       filename="live.csv", rows_total=1, is_active=True,
                       status="published")
    db.add_all([sold_batch, live]); db.flush()

    # Enough sales, with and without pools, that the comparison can resolve.
    n = 0
    for beds in (3, 4, 5):
        for floor in (120, 160, 200, 240):
            for has_pool in (False, True):
                for _ in range(4):
                    n += 1
                    db.add(PropertySold(
                        slug_id=f"s{n}", address=f"{n} Sold St", suburb="Mount Eden",
                        district="Auckland City", region="Auckland",
                        property_type="House", beds=beds, baths=2,
                        floor_area_m2=floor, land_area_m2=600,
                        sale_price=(700_000 + beds * 120_000 + floor * 1_500)
                        * (1.08 if has_pool else 1.0),
                        cv_numeric=700_000 + beds * 120_000 + floor * 1_500,
                        sold_date="2026-05-15", has_swimming_pool=has_pool,
                        import_batch_id=sold_batch.id))

    p = PropertyForSale(import_batch_id=live.id, region="Auckland",
                        suburb="Mount Eden", district="Auckland City",
                        address="1 Subject Street", asking_price=1_500_000,
                        cv_numeric=1_400_000, fair_value=1_450_000,
                        beds=4, baths=2, floor_area_m2=200, land_area_m2=600,
                        property_type="House", is_held=False,
                        has_swimming_pool=flag, description=description)
    db.add(p); db.commit(); db.refresh(p)
    return p


def _labels(res):
    return " | ".join(u.label for u in res.options)


def test_a_house_with_a_pool_is_not_told_what_a_pool_would_add(db_session):
    p = _world(db_session, flag=True)
    res = property_value_add(property_id=p.id, db=db_session)
    assert "pool" not in _labels(res).lower(), _labels(res)


def test_nor_when_only_the_listing_text_says_so(db_session):
    """The gap this closes: flag blank, pool plainly described."""
    p = _world(db_session, flag=None,
               description="Summer sorted — a heated swimming pool off the deck.")
    res = property_value_add(property_id=p.id, db=db_session)
    assert "pool" not in _labels(res).lower(), _labels(res)


def test_a_house_without_one_still_sees_the_comparison(db_session):
    """The line is useful where it applies; this is not a blanket removal."""
    p = _world(db_session, flag=False, description="Sunny three bedroom on a flat site.")
    res = property_value_add(property_id=p.id, db=db_session)
    assert "pool" in _labels(res).lower(), _labels(res)


def test_a_spa_pool_does_not_count_as_one(db_session):
    """The whole reason the text side is conservative."""
    p = _world(db_session, flag=None, description="Covered deck with a spa pool.")
    res = property_value_add(property_id=p.id, db=db_session)
    assert "pool" in _labels(res).lower(), (
        "a hot tub was treated as a swimming pool and the comparison disappeared"
    )
