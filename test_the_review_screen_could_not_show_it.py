"""Why the batch could not be audited before it went live.

    "so i couldnt audit the data before it went live why ?"

Because nothing on the review screen could contradict the wrong answer, and one
column positively confirmed it.

718 Remuera Road went to auction. It arrived classified `fixed`, with the
council valuation sitting in the Asking column. A reviewer looking at that row
saw a plausible price, a plausible margin, and — one column over, under "Price
basis" — the word **advertised**. Every fact on the screen agreed with itself.
The one value that disagreed, the feed's own `sale_method`, was read during the
load and thrown away.

And the summary above the grid did not help either. It counted rows, rejections,
holds and lookups; it never said how many listings a vendor had actually named a
price for. On the export in question that number was 1,436 of 1,586 — nine in
ten — in a market where four houses in five sell by auction, tender or
negotiation. Anybody who knows Auckland would have stopped dead at 91%. Nobody
was shown it.

So two things are added, and this is what they have to do.
"""
from __future__ import annotations

import pandas as pd

from app.ingest import ingest_for_sale, ingest_sold
from app.models import BatchType, ImportBatch, PropertyForSale
from tests.test_no_price_is_invented import _listing, _sold_frame


def _stage(db, rows):
    """An upload sitting in review — staged, not yet live."""
    ingest_sold(db, _sold_frame(), "sold.csv", region="Auckland", publish=True)
    ingest_for_sale(db, pd.DataFrame(rows), _sold_frame(), "week.csv",
                    region="Auckland", publish=False)
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value)
            .order_by(ImportBatch.id.desc()).first())


# ---- the evidence now sits beside the conclusion ----------------------------
def test_the_grid_says_how_the_house_sells(db_session):
    """THE MISSING COLUMN. Not our classification — the feed's own word, so the
    two can be seen to disagree. A grid showing only the conclusion is the model
    marking its own work."""
    from app.routers.release import _grid_row

    batch = _stage(db_session, [_listing()])
    row = _grid_row(db_session.query(PropertyForSale)
                    .filter(PropertyForSale.import_batch_id == batch.id).one())
    assert row.sale_method == "auction"
    assert row.listing_type == "auction"


def test_the_column_exists_on_the_screen_not_only_in_the_response():
    """A field the API returns and the grid never renders is not an audit."""
    from pathlib import Path

    grid = Path("../ollie-v5-frontend/components/StagedReviewGrid.tsx")
    if not grid.exists():
        import pytest
        pytest.skip("frontend not present beside the backend")
    src = grid.read_text()
    assert '"sale_method"' in src, "the review grid has no 'how it sells' column"
    assert '"listing_type"' in src


# ---- and the count that would have stopped the publish ----------------------
def test_the_summary_counts_how_many_vendors_named_a_price(db_session):
    """One number, on the screen, before the button. Four in five Auckland
    houses name no price, so a batch claiming nine in ten do is a batch reading
    search prices as asking prices."""
    from app.release import staged_summary

    rows = [_listing(slug_id=f"auction-{i}", address=f"{i} Auction Road")
            for i in range(4)]
    rows.append(_listing(slug_id="priced", address="1 Priced Street",
                         sale_method="fixed price"))
    _stage(db_session, rows)

    s = staged_summary(db_session, region="Auckland")
    assert s.forsale_rows == 5
    assert s.priced_rows == 1, "counted a price nobody named"
    assert s.unpriced_rows == 4
    assert s.priced_rows + s.unpriced_rows == s.forsale_rows


def test_the_old_behaviour_would_have_shown_five_of_five(db_session):
    """The fault, sized on the same rows. Without the sale_method column every
    one of these five reads as a vendor-named price — which is the screen a
    reviewer was given, and it had nothing wrong with it to see."""
    from app.ingest import _detect_listing_type

    rows = [_listing(slug_id=f"a{i}") for i in range(4)] + [_listing(slug_id="p")]
    without = [_detect_listing_type(r["price_display"], r["price_numeric"])
               for r in rows]
    assert without == ["fixed"] * 5

    with_it = [_detect_listing_type(r["price_display"], r["price_numeric"],
                                    r["sale_method"]) for r in rows]
    assert with_it == ["auction"] * 5
