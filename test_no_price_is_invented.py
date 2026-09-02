"""No number is presented as an asking price unless a vendor asked it.

    "No the bug is making up numbers"

718 Remuera Road goes to auction. There is no asking price — that is what an
auction IS. The feed still carries a number in its price field, $4,250,000,
which is the council valuation to the dollar, and we published it as though the
vendor had named it.

The pipeline has always had the rule: a listing that is not `fixed` has no
asking price, and `price_numeric` is cleared before ingest ever reads it. The
rule was sound and it never fired, because the classifier decided the listing
was `fixed` — it read the price text, saw "$4.25M", and never looked at the
`sale_method` column sitting next to it. One rule, and the one input that
decides whether it applies was wrong for 1,128 of 1,586 listings in a single
day's file.

So this test does not test the classifier (test_the_feed_says_how_it_sells.py
does that) and does not test the blanking rule in isolation. It tests the thing
the customer sees: a real auction row, ingested the way an upload ingests it,
and then the stored asking price read back off the database. That is the only
place the two halves are joined, and it is the join that was broken.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.ingest import ingest_for_sale, ingest_sold
from app.models import ImportBatch, PropertyForSale


def _sold_frame() -> pd.DataFrame:
    """Enough comparable sales for the run to do real work."""
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Remuera",
        "district": "Auckland City", "region": "Auckland",
        "property_type": "House", "key_bedrooms": 4, "key_bathrooms": 2,
        "key_floor_area": f"{330 + i} sqm", "key_land_area": f"{1180 + i} sqm",
        "cv_numeric": 4_200_000, "price_numeric": 4_300_000 + i * 20_000,
        "sale_price": 4_300_000 + i * 20_000,
        "land_value_numeric": 2_900_000, "improvement_value_numeric": 1_300_000,
        "type_of_title": "Freehold", "sold_date": "2026-06-01",
    } for i in range(12)])


def _listing(**over) -> dict:
    """718 Remuera Road as it actually arrives in the file."""
    row = {
        "address": "718 Remuera Road", "suburb": "Remuera",
        "district": "Auckland City", "region": "Auckland",
        "property_type": "House", "slug_id": "718-remuera-road",
        "url": "https://oneroof.co.nz/listing/718-remuera-road",
        "price_display": "$4.25M", "price_numeric": 4_250_000,
        "sale_method": "auction",
        "cv_numeric": 4_250_000, "land_value_numeric": 2_900_000,
        "improvement_value_numeric": 1_350_000,
        "key_bedrooms": 4, "key_bathrooms": 4, "key_carspaces": 2,
        "key_floor_area": 339, "key_land_area": 1200,
        "type_of_title": "Freehold",
    }
    row.update(over)
    return row


def _upload(db, rows: list[dict], filename="new_listings.csv"):
    ingest_sold(db, _sold_frame(), "sold.csv", region="Auckland", publish=True)
    ingest_for_sale(db, pd.DataFrame(rows), _sold_frame(), filename,
                    region="Auckland", publish=True)
    batch = (db.query(ImportBatch)
             .filter(ImportBatch.batch_type == "for_sale",
                     ImportBatch.is_active.is_(True))
             .order_by(ImportBatch.id.desc()).first())
    return {p.slug_id: p for p in db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch.id).all()}


# ---- the property that started it -------------------------------------------
def test_718_remuera_road_is_stored_with_no_asking_price(db_session):
    """THE BUG. It goes to auction; the file's price field holds its council
    valuation; we stored that as the asking price."""
    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    assert stored.listing_type == "auction"
    assert stored.asking_price is None, (
        f"published an asking price of ${stored.asking_price:,.0f} for a house "
        "going to auction")


def test_the_number_it_invented_was_the_council_valuation(db_session):
    """Pinned separately because it is the part that makes it indefensible: the
    figure was not merely unasked, it was the CV copied across, so the margin we
    published was measured against our own anchor."""
    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    assert stored.cv_numeric == 4_250_000
    assert stored.asking_price != 4_250_000


# ---- the ones that slipped past every other defence --------------------------
# These are real rows from the same file. The placeholder rule (asking == CV, or
# asking == last sold, to the dollar) caught 362 of the 1,128. It could never
# catch these: the number differs from the CV, so it looks like a real price.
# Nothing but the sale_method column can tell.
@pytest.mark.parametrize("slug,address,method,ask,cv", [
    ("11-whitbourne-heights", "11 Whitbourne Heights", "auction",
     960_000, 970_000),
    ("56-dignan-street", "56 Dignan Street", "auction", 5_590_000, 5_850_000),
    ("9-hona-avenue", "9 Hona Avenue", "tender", 3_410_000, 3_675_000),
    ("18-celeste-place", "18 Celeste Place", "by negotiation",
     1_510_000, 1_450_000),
])
def test_a_price_that_is_not_the_cv_is_still_not_an_asking_price(
        db_session, slug, address, method, ask, cv):
    stored = _upload(db_session, [_listing(
        slug_id=slug, address=address, sale_method=method,
        price_display=f"${ask:,}", price_numeric=ask,
        cv_numeric=cv, land_value_numeric=cv * 0.68,
        improvement_value_numeric=cv * 0.32)])[slug]
    assert stored.asking_price is None, (
        f"{address} sells by {method} — nobody asked ${ask:,}")


# ---- and the price that IS real is untouched ---------------------------------
def test_a_vendor_who_named_a_price_keeps_it(db_session):
    """The other half of the bargain. 297 listings in that file say "fixed
    price" and mean it, and blanking those would be the same fault pointing the
    other way — a house advertised at $769,000 shown as having no price."""
    stored = _upload(db_session, [_listing(
        slug_id="lot-1-14-vida-place", address="Lot 1/14 Vida Place",
        sale_method="fixed price", price_display="$4,395,000",
        price_numeric=4_395_000)])["lot-1-14-vida-place"]
    assert stored.listing_type == "fixed"
    assert stored.asking_price == 4_395_000


def test_a_file_with_no_sale_method_column_still_prices_its_listings(db_session):
    """Older files, and every other source, carry no such column. They have to
    keep working exactly as they did — an empty method is not a claim that the
    house has no price."""
    row = _listing(slug_id="93-old-format-road", address="93 Old Format Road",
                   price_display="$4,395,000", price_numeric=4_395_000)
    row.pop("sale_method")
    stored = _upload(db_session, [row])["93-old-format-road"]
    assert stored.asking_price == 4_395_000


# ---- no price means no margin, and never a guess at one ----------------------
#
#   "With out a price we can't work out if there is margin you need to stop
#    creating lies and fake things I'm so worried"
#
# Correct, and it is the whole reason the fault mattered. A margin is
# valuation MINUS what the vendor is asking. Take the asking away and there is
# no subtraction to do — so the answer is not a smaller margin or an estimated
# one, it is no margin at all. Anything else is a number about a house that
# nobody has priced.
def test_a_listing_with_no_price_carries_no_margin(db_session):
    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    assert stored.margin is None
    assert stored.is_underpriced is False


def test_it_says_why_it_has_no_margin_rather_than_leaving_it_blank(db_session):
    """A blank margin with nothing to explain it is how a missing number gets
    filled in later by somebody guessing."""
    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    assert stored.deal_block_reason
    assert "no advertised price" in stored.deal_block_reason.lower()


def test_what_we_think_it_is_worth_is_still_there(db_session):
    """The other half of it:

        "Price by negotiation we could add a value on what we think it's worth"

    That is exactly what stays. The vendor has not named a price; our valuation
    of the house is ours to give and does not depend on them. So the listing
    keeps its value, its range and its buy price, and loses only the comparison
    it never had the inputs for.
    """
    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    assert stored.fair_value, "no valuation to show for a house with no price"
    assert stored.range_low and stored.range_high, "and no range either"
    assert stored.asking_price is None


def test_but_the_buy_price_goes_with_the_asking_price(db_session):
    """The line between the two, and it is worth being exact about.

    A VALUATION is a statement about the house: this is what comparable homes
    sold for. Nothing about it needs the vendor.

    A BUY PRICE is a statement about a negotiation: what you could pay, capped
    below what is being asked. Cap it against nothing and the cap is invented —
    which is the same fault as the one this whole build is about, wearing a
    different label. So it is withheld, and the valuation carries the listing.
    """
    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    assert stored.buy_price is None


def test_the_valuation_is_not_quietly_reused_as_the_asking_price(db_session):
    """The trap in giving it a value: the value must never slide back into the
    price field and become the thing we said nobody asked."""
    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    assert stored.asking_price is None
    assert stored.asking_basis is None


def test_a_listing_with_no_price_never_reaches_a_deal_page(db_session):
    """    "If they don't have a price they shouldn't show as a deal right ?"

    Right. Checked at the page rather than at the row, because "margin is None"
    is only half the answer — what matters is whether it turns up where somebody
    goes looking for deals. It appears on the plain listing list and nowhere
    else; the house beside it, with a real advertised price, appears on both.
    """
    from app.routers.properties import _active_batch, _filtered_query

    stored = _upload(db_session, [
        _listing(),
        _listing(slug_id="priced", address="1 Priced Street",
                 sale_method="fixed price", price_display="$3,600,000",
                 price_numeric=3_600_000),
    ])
    assert stored["priced"].is_underpriced is True, "the control is not a deal"
    batch = _active_batch(db_session, "for_sale", "Auckland")

    def slugs(**kw):
        return sorted(p.slug_id for p in _filtered_query(db_session, batch, **kw))

    assert slugs(underpriced=True) == ["priced"]
    assert slugs(min_margin=0.05) == ["priced"]
    assert slugs() == ["718-remuera-road", "priced"]


def test_no_deal_flag_of_any_kind_survives_the_missing_price(db_session):
    """    "We can still put valuation on houses we just don't flag them as
           deals"

    Both halves, checked together. The valuation, the range and the rent
    estimate are ours to give and stay. Every FLAG goes, because a flag is what
    puts a listing on a deal page and a deal page is a recommendation to act.

    is_underpriced always needed an asking price — a discount is measured
    against one. is_cashflow_positive did not: it was cashflow > 0, worked out
    from a purchase price we deliberately do not show.
    """
    stored = _upload(db_session, [_listing()])["718-remuera-road"]

    assert stored.fair_value                       # kept: ours to say
    assert stored.est_weekly_rent                  # kept: ours to say
    assert stored.is_underpriced is False
    assert stored.is_cashflow_positive is False
    assert stored.is_subdividable is False
    assert stored.margin is None


def test_every_budget_on_the_site_means_the_same_thing():
    """One rule, N callers — the failure that keeps recurring here.

    A buyer's budget is asked for in four places: the deal lists, a saved wish
    list, Ollie's own search, and the preferences behind Ollie's Hunt. Each had
    written the comparison out for itself against asking_price, so making the
    price honest in three of them left the fourth answering "under $1.5M" with a
    fifth of the market and nothing on screen saying so — which is exactly how
    the first three came to disagree in the first place.

    So this does not list the callers. It SEARCHES for the pattern, across every
    module in the app, and fails on any comparison of asking_price against a
    min/max price bound that is not the shared rule. The next one gets caught
    when it is written rather than when a customer finds it.
    """
    import re
    from pathlib import Path

    from app.models import BUDGET_PRICE

    assert BUDGET_PRICE is not None

    # asking_price compared against anything whose name says it is a price
    # bound — max_price, min_price, wl.max_price, p["min_price"], and so on.
    pattern = re.compile(
        r"asking_price\s*(?:<=|>=|<|>)\s*[A-Za-z_][\w.\[\]\"']*(?:min|max)_price",
        re.IGNORECASE)
    offenders = []
    for path in sorted(Path("app").rglob("*.py")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path}:{i}: {line.strip()}")
    assert offenders == [], (
        "a budget measured on the asking price alone — four listings in five "
        "have none. Use models.BUDGET_PRICE:\n  " + "\n  ".join(offenders))

    # And the callers we know about are actually using it.
    for path in ("app/routers/properties.py", "app/routers/wishlists.py",
                 "app/routers/preferences.py", "app/assistant/tools.py"):
        assert "BUDGET_PRICE" in Path(path).read_text(), \
            f"{path} does not use the shared rule"


# ---- the evidence is kept, so this stays fixed -------------------------------
def test_the_feeds_own_word_is_stored(db_session):
    """It was read during the load and thrown away. That is why the fault could
    not be repaired in place: every listing already on the site was filed as
    `fixed` and there was nothing left to re-classify it from."""
    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    assert stored.sale_method == "auction"


def test_a_repriced_listing_is_classified_from_the_method_not_the_verdict(db_session):
    """Re-running the pricing rebuilds each row from the database, and it has to
    reach the same answer. Deriving it from listing_type would be deriving the
    conclusion from itself — a listing filed wrongly once would stay wrong
    through every re-run."""
    from app import ingest

    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    scrape = ingest._row_to_scrape(stored)
    assert scrape["sale_method"] == "auction"
    assert ingest._detect_listing_type(scrape["price_display"],
                                       scrape["price_numeric"],
                                       scrape["sale_method"]) == "auction"


def test_re_running_the_pricing_clears_a_price_that_was_already_stored(db_session):
    """Re-pricing has to be a REPAIR, not just a recalculation.

    Every listing loaded before this was fixed is sitting in the database now
    with an invented asking price on it. "Re-run pricing" is the button an
    operator reaches for, and until now it would have re-classified the row as
    an auction and left the number underneath it untouched — the two columns
    disagreeing, on the site, indefinitely.
    """
    from app.models import ImportBatch
    from app.reprice import reprice_batch

    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    batch_id = stored.import_batch_id
    # Put the row back the way the old build left it.
    stored.asking_price = 4_250_000
    stored.listing_type = "fixed"
    db_session.commit()

    res = reprice_batch(db_session, batch_id, region="Auckland", commit=True)
    assert not res.error, res.error

    # Read it back rather than refreshing: the re-price empties the session
    # between chunks so it can hold a whole batch, and the object handed out
    # above is no longer attached to it.
    after = (db_session.query(PropertyForSale)
             .filter(PropertyForSale.slug_id == "718-remuera-road").one())
    assert after.listing_type == "auction"
    assert after.asking_price is None


# ---- what the customer is shown ---------------------------------------------
def test_the_listing_still_reaches_the_site_without_a_price(db_session):
    """Clearing the price must not clear the listing. The house is for sale, it
    is worth what the model says it is worth, and it belongs on the site — with
    no asking price, which is exactly what its own advertisement says."""
    from app.release import _hold_reason, hold_flagged_rows

    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    assert stored.fair_value or stored.market_value, \
        "the house lost its valuation along with its invented price"

    # The pre-publish hold pass, which is what actually decides whether a
    # customer ever sees it. Held rows are filtered out of every list.
    assert _hold_reason(stored) is None, _hold_reason(stored)
    hold_flagged_rows(db_session, stored.import_batch_id)
    after = (db_session.query(PropertyForSale)
             .filter(PropertyForSale.slug_id == "718-remuera-road").one())
    assert after.is_held is False, after.hold_reason


def test_the_whole_book_is_not_held_for_want_of_a_price(db_session):
    """The trap this fix sets, at full size.

    "Below $10,000 margin" held anything it could not measure, and before today
    every auction listing could be measured — against a number the scraper had
    invented. Take the invented numbers away and that rule quietly becomes "hold
    four listings in five", which would empty the site on the next publish and
    look nothing like the fix that caused it.
    """
    from app.release import BELOW_MARGIN_REASON, _hold_reason

    rows = [_listing(slug_id=f"auction-{i}", address=f"{i} Auction Road")
            for i in range(6)]
    rows.append(_listing(slug_id="priced", address="1 Priced Street",
                         sale_method="fixed price"))
    stored = _upload(db_session, rows)

    held = [s for s in stored.values()
            if _hold_reason(s) == BELOW_MARGIN_REASON and s.asking_price is None]
    assert held == [], (
        f"{len(held)} of {len(stored)} listings held for having no asking price")


def test_a_buyer_with_a_budget_still_finds_it(db_session):
    """The trap on the other side of this fix.

    Four listings in five have no asking price now, and the budget filter read
    asking_price alone. Left as it was, a buyer typing "$5M or under" would have
    been shown the fifth of the market that advertises a number and nothing
    else — every auction property in their range silently gone. Which is how a
    correct fix becomes tomorrow's bug report.
    """
    from app.routers.properties import _active_batch, _filtered_query

    stored = _upload(db_session, [_listing()])["718-remuera-road"]
    batch = _active_batch(db_session, "for_sale", "Auckland")
    value = stored.fair_value or stored.market_value

    found = _filtered_query(db_session, batch, max_price=value + 500_000).all()
    assert [p.slug_id for p in found] == ["718-remuera-road"]

    # ...and a budget genuinely below it still excludes it.
    below = _filtered_query(db_session, batch, max_price=value * 0.5).all()
    assert below == []
