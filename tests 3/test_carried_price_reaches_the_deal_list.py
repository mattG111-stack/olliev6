"""A rescued listing has to reach the page, not just the row.

    "if a listing that had a price before and now in the latest file doesn't
     have a price we re-add that price. Have you fixed those listings cause
     they haven't shown today?"

The carry-forward has been fixed twice and reported fixed twice, and both times
the check stopped one step short of the question actually being asked. The first
fix put the old price on the row and the pricing run ignored it. The second made
the pricing run use it — and nothing then checked whether the listing turns up
where somebody would look for it.

So this runs the whole way to the end: two loads, the price stage's own
carry-forward, the pricing run, and then the LIST ENDPOINT, filtered the way the
Underpriced page filters it.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.ingest import ingest_for_sale, ingest_sold
from app.models import ImportBatch, PropertyForSale
from app.prior_price import DERIVED_BASIS, carry_forward_prices
from app.reprice import reprice_batch


def _sold_frame() -> pd.DataFrame:
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Epsom", "district": "Epsom",
        "region": "Auckland", "property_type": "House",
        "key_bedrooms": 4, "key_bathrooms": 2,
        "key_floor_area": f"{200 + i} sqm", "key_land_area": f"{700 + i} sqm",
        "cv_numeric": 3_200_000, "price_numeric": 3_400_000 + i * 15_000,
        "sale_price": 3_400_000 + i * 15_000, "land_value_numeric": 2_000_000,
        "improvement_value_numeric": 1_200_000, "type_of_title": "Freehold",
        "sold_date": "2026-06-01",
    } for i in range(12)])


def _listing(price) -> dict:
    return {
        "address": "150 The Drive", "suburb": "Epsom", "district": "Epsom",
        "region": "Auckland", "property_type": "House", "slug_id": "150-the-drive",
        "cv_numeric": 3_275_000, "price_numeric": price,
        "key_floor_area": 210, "key_land_area": 720, "key_bedrooms": 4,
        "key_bathrooms": 2, "type_of_title": "Freehold",
        "land_value_numeric": 2_000_000, "improvement_value_numeric": 1_275_000,
    }


def _week(db, price, filename):
    return ingest_for_sale(db, pd.DataFrame([_listing(price)]), _sold_frame(),
                           filename, region="Auckland", publish=True)


def _run_price_stage(db, batch_id: int) -> None:
    """What the Re-run pricing button does, in the order it does it."""
    carry_forward_prices(db, batch_id, "Auckland")
    res = reprice_batch(db, batch_id, region="Auckland", commit=True)
    assert not res.error, res.error


def test_a_rescued_listing_reaches_the_underpriced_list(db_session):
    """The whole question, end to end.

    Week one it is on the market at $2,580,000. Week two the vendor withdraws
    the price and the scraper writes the council valuation into the field. The
    listing has to come out of week two priced from the vendor's own last
    figure, and it has to be THERE — on the list, with a margin, where somebody
    goes looking for it.
    """
    from app.routers.properties import _active_batch, _filtered_query

    ingest_sold(db_session, _sold_frame(), "sold.csv", region="Auckland",
                publish=True)
    _week(db_session, 2_580_000, "week1.csv")
    _week(db_session, 3_275_000, "week2.csv")          # asking == CV

    batch = (db_session.query(ImportBatch)
             .filter(ImportBatch.batch_type == "for_sale")
             .order_by(ImportBatch.id.desc()).first())
    bid = batch.id
    _run_price_stage(db_session, bid)

    row = (db_session.query(PropertyForSale)
           .filter(PropertyForSale.import_batch_id == bid).one())
    assert row.asking_basis == DERIVED_BASIS, (
        f"not priced from the carried figure: {row.asking_basis!r}")
    assert row.asking_price == 2_500_000.0

    # And now the part nothing has ever checked: the list the page reads.
    assert _active_batch(db_session, "for_sale", "Auckland") == bid
    on_list = _filtered_query(db_session, bid, underpriced=True).all()
    assert [p.address for p in on_list] == ["150 The Drive"], (
        "the price came back and the listing still does not reach the deal list")


def test_the_placeholder_guard_does_not_throw_it_out_again(db_session):
    """The trap this walked into once already.

    The asking price is derived, and a guard exists to refuse listings whose
    asking is really the council valuation. It has to read the PROVENANCE, not
    just the number — a carried price that happens to round near the CV is still
    a real figure a vendor named.
    """
    from app.routers.properties import _filtered_query

    ingest_sold(db_session, _sold_frame(), "sold.csv", region="Auckland",
                publish=True)
    _week(db_session, 2_580_000, "week1.csv")
    _week(db_session, 3_275_000, "week2.csv")
    batch = (db_session.query(ImportBatch)
             .filter(ImportBatch.batch_type == "for_sale")
             .order_by(ImportBatch.id.desc()).first())
    bid = batch.id
    _run_price_stage(db_session, bid)

    assert _filtered_query(db_session, bid).count() == 1, (
        "the rescued listing is being hidden as a placeholder asking")


def test_with_no_earlier_load_there_is_nothing_to_rescue(db_session):
    """And this is the answer to "why did none of them show today".

    A listing can only be given last week's price if last week's ROW still
    exists. Every week the region check threw the file away, the history the
    carry-forward needs went with it — so the first load after that fix has
    nothing behind it, however long the house has really been listed. It takes
    two consecutive loads that both survived.
    """
    ingest_sold(db_session, _sold_frame(), "sold.csv", region="Auckland",
                publish=True)
    _week(db_session, 3_275_000, "week1.csv")          # placeholder from day one

    batch = (db_session.query(ImportBatch)
             .filter(ImportBatch.batch_type == "for_sale")
             .order_by(ImportBatch.id.desc()).first())
    assert carry_forward_prices(db_session, batch.id, "Auckland") == 0
