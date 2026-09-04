"""    "And where it gives a price like this we can use it as a asking price"

10 Fernbird Place, Massey. The advertisement reads **Enquiries over $699,000**.
We showed nothing at all.

That figure is not a search price and it is not the council valuation. An agent
typed it, a vendor agreed to it, and it is printed on the listing — so throwing
it away with the invented numbers was the same fault pointing the other way:
withholding something said out loud.

It is also not a firm asking price, and that distinction is not decoration. The
house will sell ABOVE $699,000; that is the entire purpose of the wording. So it
is published, and:

  * the valuation is NOT anchored to it — "guide" stays out of
    LISTING_TYPES_WITH_ASKING, so the asking x 0.95 path never sees it;
  * no margin is measured against it, because valuation minus a floor flatters
    every listing of this kind;
  * our buy price is not capped at it, because "what you can pay" capped at a
    starting figure lands below what the place plainly costs.

Both readings have to agree: the feed's own sale_method column when there is
one, and the price line for every source without one.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.ingest import _detect_listing_type as detect
from app.ingest import ingest_for_sale, ingest_sold
from app.models import ImportBatch, PropertyForSale
from app.prior_price import GUIDE_BASIS


# ---- reading it, both ways --------------------------------------------------
@pytest.mark.parametrize("method", [
    "enquiries over", "offers over", "buyer enquiry over",
    "buyer budget over", "negotiable from", "in excess of",
])
def test_a_stated_floor_with_a_number_is_a_guide(method):
    assert detect("Enquiries over $699,000", 699_000, method) == "guide"


def test_the_same_words_in_the_price_line_read_the_same(method="Enquiries over $699,000"):
    """Portals and older files carry no sale_method column. Before this they
    read as `fixed` — a floor on the asking x 0.95 valuation path."""
    assert detect(method, 699_000, None) == "guide"
    assert detect("Offers over $1,200,000", 1_200_000, None) == "guide"


def test_a_floor_with_no_number_names_nothing():
    """"Enquiries over" and nothing after it is a by-negotiation listing."""
    assert detect("Enquiries over", None, "enquiries over") == "negotiation"


def test_a_plain_price_is_still_a_plain_price():
    assert detect("$769,000", 769_000, "fixed price") == "fixed"
    assert detect("$829,000 Negotiable", 829_000, None) == "fixed"


def test_the_valuation_is_never_anchored_to_a_floor():
    """The reason this is its own type rather than just `fixed`."""
    from app.pricing.glm import LISTING_TYPES_WITH_ASKING

    assert "guide" not in LISTING_TYPES_WITH_ASKING


# ---- and what a customer is shown -------------------------------------------
def _sold():
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Massey", "district": "Waitakere City",
        "region": "Auckland", "property_type": "House", "key_bedrooms": 3,
        "key_bathrooms": 1, "key_floor_area": f"{120 + i} sqm",
        "key_land_area": f"{600 + i * 10} sqm", "cv_numeric": 800_000,
        "price_numeric": 820_000 + i * 10_000, "sale_price": 820_000 + i * 10_000,
        "land_value_numeric": 500_000, "improvement_value_numeric": 300_000,
        "type_of_title": "Freehold", "sold_date": "2026-06-01",
    } for i in range(12)])


def _row(**over):
    row = dict(address="10 Fernbird Place", suburb="Massey",
               district="Waitakere City", region="Auckland",
               property_type="House", slug_id="10-fernbird-place",
               url="https://oneroof.co.nz/10-fernbird-place",
               price_display="Enquiries over $699,000", price_numeric=699_000,
               sale_method="enquiries over", cv_numeric=800_000,
               land_value_numeric=500_000, improvement_value_numeric=300_000,
               key_bedrooms=3, key_bathrooms=1, key_carspaces=1,
               key_floor_area=130, key_land_area=650, type_of_title="Freehold")
    row.update(over)
    return row


def _load(db, rows):
    ingest_sold(db, _sold(), "sold.csv", region="Auckland", publish=True)
    ingest_for_sale(db, pd.DataFrame(rows), _sold(), "live.csv",
                    region="Auckland", publish=True)
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == "for_sale", ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    return {p.slug_id: p for p in db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == b.id).all()}


def test_the_number_reaches_the_listing(db_session):
    """THE ASK. It showed nothing; the advertisement says $699,000."""
    p = _load(db_session, [_row()])["10-fernbird-place"]
    assert p.listing_type == "guide"
    assert p.asking_price == 699_000


def test_and_it_says_the_number_is_a_floor(db_session):
    """The basis travels with it. A guide that cannot be told from an asking
    price is the whole risk in publishing one."""
    p = _load(db_session, [_row()])["10-fernbird-place"]
    assert p.asking_basis == GUIDE_BASIS


def test_no_margin_is_measured_against_it(db_session):
    """Valuation minus a floor flatters every listing of this kind — the house
    sells above the number, which is what the wording is for."""
    p = _load(db_session, [_row()])["10-fernbird-place"]
    assert p.margin is None
    assert p.is_underpriced is False
    assert p.deal_block_reason and "guide" in p.deal_block_reason.lower()


def test_our_buy_price_is_not_capped_at_the_floor(db_session):
    """"What you can pay" capped at a starting figure lands below what the place
    plainly costs. Ours comes from the comps instead."""
    p = _load(db_session, [_row()])["10-fernbird-place"]
    assert p.buy_price, "no estimate at all"
    assert p.buy_price > 699_000 * 0.95, (
        f"buy price ${p.buy_price:,.0f} is capped at the ${699_000:,} floor")


def test_the_valuation_still_stands_on_its_own(db_session):
    p = _load(db_session, [_row()])["10-fernbird-place"]
    assert p.fair_value
    # asking x 0.95 would be $664,050 — the give-away that it took the wrong path.
    assert abs(p.fair_value - 699_000 * 0.95) > 1_000
