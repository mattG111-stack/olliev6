"""Trade Me's export fills our gaps. It does not price anything.

Their file is a large list of settled Auckland sales carrying a price, a council
valuation, a floor area, coordinates and an ownership type — all fields we have
holes in. It carries no bedrooms, no bathrooms, no days on market and no sale
method, which is why it fills rather than replaces.

It also carries their own estimate, and that estimate is not an estimate. Across
54,692 sales it lands a median 1.2% from the actual sale price, and the gap runs
smoothly from -2.2% for sales in August 2024 to 0.0% for sales happening now:
they take the sale price and index it forward. So it is displayed as "Trade Me
says" and never reaches a valuation, a margin or a comparison.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app import trademe
from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold


def _their_row(**over):
    row = {
        "address": "12 Donovan Street, Blockhouse Bay, Auckland City",
        "suburb": "Blockhouse Bay", "city": "Auckland City",
        "latitude": -36.92, "longitude": 174.70,
        "property_type": "House", "property_type_confidence": "high",
        "ownership_type": "Freehold",
        "sale_date": "6/13/2026", "sale_price": 1_250_000.0,
        "sale_display_price": "$1,250,000",
        "floor_area_m2": 180.0, "land_area_m2": 620.0,
        "est_value": "$1.24M", "est_value_low": "$1.17M", "est_value_high": "$1.31M",
        "est_value_date": "6-Aug-26",
        "capital_value": 1_200_000.0, "land_value": 800_000.0,
        "improvement_value": 400_000.0, "cv_revision_date": "1-May-24",
        "cover_image_url": "https://example.invalid/1.jpg",
    }
    row.update(over)
    return row


def _ours(db, **over):
    live = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                       filename="live.csv", rows_total=1, is_active=True,
                       status="published")
    db.add(live); db.flush()
    fields = {
        "import_batch_id": live.id, "region": "Auckland", "suburb": "Blockhouse Bay",
        "address": "12 Donovan Street, Blockhouse Bay, Auckland City, Auckland",
        "asking_price": 1_300_000, "beds": 4, "baths": 2, "is_held": False,
        "property_type": "House",
    }
    fields.update(over)
    p = PropertyForSale(**fields)
    db.add(p); db.commit(); db.refresh(p)
    return p


def _sold(db, **over):
    batch = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                        filename="sold.csv", rows_total=1, is_active=True,
                        status="published")
    db.add(batch); db.flush()
    fields = {
        "import_batch_id": batch.id, "region": "Auckland", "suburb": "Blockhouse Bay",
        "address": "12 Donovan Street, Blockhouse Bay, Auckland City, Auckland",
        "sale_price": 1_250_000, "sold_date": "2026-06-25", "beds": 4, "baths": 2,
        "property_type": "House", "slug_id": "abc",
    }
    fields.update(over)
    p = PropertySold(**fields)
    db.add(p); db.commit(); db.refresh(p)
    return p


def _frame(*rows):
    return trademe.load(pd.DataFrame(list(rows)))


# ---- reading their file -----------------------------------------------------
def test_their_dates_are_month_first():
    """9/14/2024 is September. Read day-first it is not a date at all, and
    4/30/2026 quietly becomes something else entirely."""
    f = _frame(_their_row(sale_date="9/14/2024"), _their_row(sale_date="4/30/2026"),
               _their_row(sale_date="6/13/2025"))
    assert list(f.sold_date) == ["2024-09-14", "2026-04-30", "2025-06-13"]


def test_a_date_that_reads_either_way_still_goes_to_their_order():
    """3/4/2026 is 4 March in their file. Day-first it would be 3 April."""
    assert list(_frame(_their_row(sale_date="3/4/2026")).sold_date) == ["2026-03-04"]


def test_their_shorthand_money_is_read():
    f = _frame(_their_row(est_value="$815K", est_value_low="$770K",
                          est_value_high="$1.31M"))
    assert f.tm_valuation.iloc[0] == 815_000
    assert f.tm_valuation_low.iloc[0] == 770_000
    assert f.tm_valuation_high.iloc[0] == 1_310_000


def test_a_type_they_are_unsure_about_is_not_taken(db_session):
    """A guessed type moves a property into a different comparable set."""
    f = _frame(_their_row(property_type="Townhouse", property_type_confidence="medium"))
    assert pd.isna(f.property_type.iloc[0])

    p = _ours(db_session, property_type=None)
    trademe.fill(db_session, f, region="Auckland")
    db_session.refresh(p)
    assert p.property_type is None, "a type they were only half sure of was written in"


def test_addresses_match_across_two_sources_that_write_them_differently():
    """Theirs stops at the city; ours carries the region too."""
    assert (trademe.address_key("1 Abernethy Way, Patumahoe, Pukekohe", "Patumahoe")
            == trademe.address_key("1 Abernethy Way, Patumahoe, Auckland", "Patumahoe"))
    assert (trademe.address_key("3/107 Donovan St", "Blockhouse Bay")
            == trademe.address_key("3 / 107 Donovan St,", "blockhouse bay"))


def test_the_same_street_in_two_suburbs_is_two_addresses():
    assert (trademe.address_key("12 Queen Street", "Onehunga")
            != trademe.address_key("12 Queen Street", "Northcote"))


# ---- filling ----------------------------------------------------------------
def test_it_fills_a_hole(db_session):
    p = _ours(db_session, floor_area_m2=None, cv_numeric=None)
    res = trademe.fill(db_session, _frame(_their_row()), region="Auckland")
    db_session.refresh(p)
    assert p.floor_area_m2 == 180.0
    assert p.cv_numeric == 1_200_000.0
    assert res.filled["floor_area_m2"] == 1


def test_it_never_overwrites_what_we_already_hold(db_session):
    """Ours came from a source that also carries beds, baths and sale method.
    A field we have is one this file cannot improve."""
    p = _ours(db_session, floor_area_m2=175.0, cv_numeric=1_150_000.0)
    trademe.fill(db_session, _frame(_their_row()), region="Auckland")
    db_session.refresh(p)
    assert p.floor_area_m2 == 175.0, "our own floor area was overwritten"
    assert p.cv_numeric == 1_150_000.0, "our own CV was overwritten"


def test_it_adds_no_rows(db_session):
    """A property they hold and we do not is not ours to invent."""
    _ours(db_session)
    before = db_session.query(PropertyForSale).count() + db_session.query(PropertySold).count()
    res = trademe.fill(db_session, _frame(
        _their_row(), _their_row(address="99 Nowhere Road, Elsewhere", suburb="Elsewhere")),
        region="Auckland")
    after = db_session.query(PropertyForSale).count() + db_session.query(PropertySold).count()
    assert before == after
    assert res.unmatched == 1


def test_a_dry_run_changes_nothing(db_session):
    p = _ours(db_session, floor_area_m2=None)
    res = trademe.fill(db_session, _frame(_their_row()), region="Auckland", dry_run=True)
    db_session.refresh(p)
    assert p.floor_area_m2 is None
    assert res.filled["floor_area_m2"] == 1, "a dry run must still report what it would do"


def test_a_sold_record_with_no_price_can_get_one(db_session):
    p = _sold(db_session, sale_price=None)
    trademe.fill(db_session, _frame(_their_row()), region="Auckland")
    db_session.refresh(p)
    assert p.sale_price == 1_250_000.0


# ---- their figure -----------------------------------------------------------
def test_their_figure_is_stored_for_display(db_session):
    p = _ours(db_session)
    trademe.fill(db_session, _frame(_their_row()), region="Auckland")
    db_session.refresh(p)
    assert p.tm_valuation == 1_240_000
    assert p.tm_valuation_low == 1_170_000 and p.tm_valuation_high == 1_310_000
    assert p.tm_valuation_date == "6-Aug-26"


def test_their_figure_never_touches_ours(db_session):
    """It is not a second opinion — it is the sale price carried forward."""
    p = _ours(db_session, fair_value=1_180_000, cv_numeric=1_100_000)
    trademe.fill(db_session, _frame(_their_row()), region="Auckland")
    db_session.refresh(p)
    assert p.fair_value == 1_180_000, "their figure reached our valuation"
    assert p.cv_numeric == 1_100_000, "their figure reached the council valuation"
    assert p.third_party_valuation is None, "their figure was filed as an independent estimate"


def test_a_current_figure_does_not_land_on_a_1999_sale(db_session):
    """One address, five sales on file. Their number describes the house today.

    Attached to every row it prints "Trade Me says $1.57M" beside a sale from
    last century.
    """
    rows = [_sold(db_session, sold_date=d, sale_price=p, slug_id=f"s{i}")
            for i, (d, p) in enumerate([("1999-05-31", 335_000), ("2012-01-31", 650_000),
                                        ("2026-06-25", 1_250_000)])]
    trademe.fill(db_session, _frame(_their_row()), region="Auckland")
    for r in rows:
        db_session.refresh(r)
    newest = max(rows, key=lambda r: r.sold_date)
    assert newest.tm_valuation == 1_240_000
    assert all(r.tm_valuation is None for r in rows if r is not newest), (
        "a current figure was attached to a historic sale"
    )


# ---- the overlap ------------------------------------------------------------
def test_the_same_sale_dated_differently_is_reported(db_session):
    """Theirs looks like the agreement date, ours the settlement — weeks apart.

    Our duplicate check is (slug_id, sold_date) and this file has neither the
    same slug nor the same date, so the difference has to be surfaced rather
    than left to turn into two sales of one house.
    """
    _sold(db_session, sold_date="2026-06-25")
    res = trademe.fill(db_session, _frame(_their_row(sale_date="6/13/2026")),
                       region="Auckland")
    assert res.conflicts and "2026-06-25" in res.conflicts[0]
    assert "2026-06-13" in res.conflicts[0]


def test_two_different_sales_of_one_house_are_not_called_a_conflict(db_session):
    """A 2020 sale and a 2026 sale are two sales, not one disagreement."""
    _sold(db_session, sold_date="2020-08-28", sale_price=900_000)
    res = trademe.fill(db_session, _frame(_their_row(sale_date="6/13/2026")),
                       region="Auckland")
    assert res.conflicts == []
