"""The headline and the workings under it have to be the same calculation.

The screenshots: a deal card reading "+19 lots · $4.69M net gain", and the
subdivision panel on that property's own page with a dash through every line —
total subdivided value, gross sales, less purchase, best net gain, all "—".

Two buy prices wearing one name is how that happened.

The pricing run computed the subdivision from `bp.buy_price`, the comp engine's
own estimate, which exists for any listing with comparable land around it. It
published the profit that came out. But the figure STORED as buy_price was
withheld on a listing with no advertised price — my change, earlier the same
day — so the property page, which recomputes from the stored value, was handed
nothing and could not produce a single line of the maths that the card was
already quoting.

    "use our estimate buy price"

So it is stored and shown. It was never a claim about what a vendor is asking —
it is derived from what comparable land sells for — and withholding it did not
stop us relying on it. It only stopped anyone seeing what we relied on.
"""
from __future__ import annotations

import pandas as pd

from app.ingest import ingest_for_sale, ingest_sold
from app.models import ImportBatch, PropertyForSale

THAB = "Residential - Terrace Housing and Apartment Building Zone"
MHU = "Residential - Mixed Housing Urban Zone"


def _sold():
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Glen Eden",
        "district": "Waitakere City", "region": "Auckland",
        "property_type": "House", "key_bedrooms": 3, "key_bathrooms": 1,
        "key_floor_area": f"{140 + i} sqm", "key_land_area": f"{800 + i * 20} sqm",
        "cv_numeric": 1_900_000, "price_numeric": 1_950_000 + i * 20_000,
        "sale_price": 1_950_000 + i * 20_000, "land_value_numeric": 1_400_000,
        "improvement_value_numeric": 500_000, "type_of_title": "Freehold",
        "sold_date": "2026-06-01",
    } for i in range(12)])


def _site(**over):
    """1 Pleasant Road: by negotiation, big, and zoned for terraces."""
    row = dict(address="1 Pleasant Road", suburb="Glen Eden",
               district="Waitakere City", region="Auckland",
               property_type="House", slug_id="1-pleasant-road",
               url="https://oneroof.co.nz/1-pleasant-road",
               price_display="By negotiation", price_numeric=1_950_000,
               sale_method="by negotiation", zoning=THAB,
               cv_numeric=1_950_000, land_value_numeric=1_500_000,
               improvement_value_numeric=450_000,
               key_bedrooms=2, key_bathrooms=1, key_carspaces=1,
               key_floor_area=150, key_land_area=2_400,
               type_of_title="Freehold")
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


def test_a_development_site_with_no_price_still_has_a_buy_price(db_session):
    """THE ONE THE SCREENSHOTS SHOWED. Without it the page under the headline
    is a column of dashes."""
    p = _load(db_session, [_site()])["1-pleasant-road"]
    assert p.asking_price is None, "still no invented asking price"
    assert p.buy_price and p.buy_price > 0, "no figure to develop the site from"


def test_the_headline_gain_and_the_workings_come_from_one_number(db_session):
    """The card quotes best_net_gain; the panel recomputes from the stored buy
    price. If those are two different numbers the page contradicts itself, which
    is exactly what it did."""
    from app.routers.properties import ScenarioIn, subdivision_scenario

    p = _load(db_session, [_site()])["1-pleasant-road"]
    if p.best_net_gain is None:
        # Nothing claimed on the card — then nothing may appear underneath
        # either, and that is consistent. The fault was one without the other.
        out = subdivision_scenario(p.id, ScenarioIn(), db=db_session)
        assert out.subdivision_profit in (None, 0)
        return

    out = subdivision_scenario(p.id, ScenarioIn(), db=db_session)
    assert out.buy_price, "the panel has no purchase figure to work from"
    assert out.gross_sales, "a headline gain over a blank set of workings"
    assert out.subdivision_profit is not None
    # Same calculation, same inputs, so the same answer within rounding.
    assert abs(out.subdivision_profit - p.best_net_gain) <= 1


def test_a_corrupted_asking_still_produces_no_buy_price(db_session):
    """The rule that was right and stays — but it is about a CORRUPTED number,
    not a qualified one, and this test used to conflate the two.

    A placeholder is the scraper's doing: the council valuation copied into the
    price field, to the dollar. Nobody asked it, so 0.95 x it lands miles off
    (169 Te Oneroa Way: $978k off a placeholder $1.03M ask on a $2.5M-CV new
    build) and there is no buy price to be had from it.

    "Offers over $1,600,000" is the opposite case and it was failing here for
    the wrong reason: that figure IS advertised, the vendor named it, and our
    buy price for it comes from the comps rather than from 0.95 x the floor.
    That case now lives in test_a_guide_price_is_a_real_number.py.
    """
    p = _load(db_session, [_site(
        slug_id="placeholder", address="2 Placeholder Road",
        sale_method="fixed price", price_display="$1,950,000",
        price_numeric=1_950_000)])["placeholder"]      # == its CV, to the dollar
    assert p.asking_price is None or p.buy_price is None


def test_a_house_we_could_not_value_still_produces_no_buy_price(db_session):
    """No valuation, no basis for what to pay — unchanged."""
    p = _load(db_session, [_site(slug_id="unpriced", address="3 Unpriced Road",
                                 zoning=MHU, cv_numeric=None,
                                 land_value_numeric=None,
                                 improvement_value_numeric=None)])
    row = p.get("unpriced")
    if row is not None and row.fair_value is None:
        assert row.buy_price is None


# ---- and it is labelled as ours, wherever it appears -------------------------
#
#     "we should have our 'estimate buy price' if thats what it is"
#
# It is. It comes from what comparable land sells for, not from anything a
# vendor said, and now that it is published on a listing with no advertised
# price the label is the only thing standing between "our estimate" and "the
# price". English mostly said "Est. buy price" already. Four of the six
# languages said plain "buy price" — the caveat existed only for people reading
# English — and the subdivision calculator, the screen the whole complaint came
# from, said plain "Buy price" in all six.
def test_every_buy_price_label_says_it_is_an_estimate():
    import re
    from pathlib import Path

    i18n = Path("../ollie-v5-frontend/lib/i18n.tsx")
    if not i18n.exists():
        import pytest
        pytest.skip("frontend not present beside the backend")
    src = i18n.read_text()

    # Every screen that puts the figure in front of somebody.
    keys = ["deal.buyPrice",      # the deal card
            "prop.buyPrice",      # the property hero
            "prop.buyPriceRow",   # the property pricing table
            "ptable.buyPrice",    # the all-properties table
            "subcalc.buyPrice"]   # the subdivision calculator
    # "estimated", however each language spells it.
    marks = {"en": ("est.", "estimated"), "zh": ("预估",), "ta": ("மதிப்பிட்ட",),
             "gu": ("અંદાજિત",), "pa": ("ਅਨੁਮਾਨਿਤ",), "hi": ("अनुमानित",)}

    missing = []
    for key in keys:
        m = re.search(re.escape(f'"{key}": ') + r"\{([^\n]*)\},", src)
        assert m, f"{key} is not in the string table"
        for lang, words in marks.items():
            lm = re.search(rf'{lang}:\s*"([^"]*)"', m.group(1))
            assert lm, f"{key} has no {lang}"
            if not any(w.lower() in lm.group(1).lower() for w in words):
                missing.append(f'{key}/{lang}: "{lm.group(1)}"')
    assert missing == [], (
        "a buy price shown without saying it is our estimate:\n  "
        + "\n  ".join(missing))
