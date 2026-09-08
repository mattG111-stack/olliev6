"""OneRoof gets its own columns, and stops destroying Hougarden's figure.

    "and its pulling data from one roof and doing what with that data and its
     missing information"

WHAT IT WAS DOING WITH IT

OneRoof's estimate was written into `third_party_valuation` — which is the
HOUGARDEN figure, the one that arrives with the weekly for-sale feed. The
mapping carried a comment saying it would only fill a blank. Nothing enforced
that: fill.py refreshes an estimate unconditionally and on purpose, because a
portal's estimate moves with their index.

So every enrich run overwrote Hougarden's number with OneRoof's, permanently.
Three consequences, none of them visible from the outside:

  the original Hougarden figure was destroyed, not kept anywhere
  a property page labelled one portal could be showing another's number
  the accuracy panel measured "us versus Hougarden" against a mixture of the
  two, depending on which properties happened to have been enriched

Every other portal — homes.co.nz, realestate.co.nz, Trade Me, CoreLogic — has
its own columns. OneRoof did not, for no better reason than that nobody added
them.
"""
from __future__ import annotations

import pytest

from app.models import PropertyForSale
from app.portals import ESTIMATE_COLUMNS


def _row(**kw) -> PropertyForSale:
    """An unsaved listing. The boolean flags are set explicitly because a row
    that has never been flushed has not had its column defaults applied yet,
    and the response model takes them as real booleans."""
    return PropertyForSale(id=1, address="1 Test Rd", suburb="Riverhead",
                           is_underpriced=False, is_cashflow_positive=False,
                           is_subdividable=False, **kw)


def test_oneroof_no_longer_writes_into_the_hougarden_columns():
    mid, low, high, url = ESTIMATE_COLUMNS["oneroof"]
    assert mid == "oneroof_valuation"
    assert "third_party" not in mid
    assert "third_party" not in (low or "")
    assert "third_party" not in (high or "")


def test_the_columns_exist_on_the_model():
    for c in ("oneroof_valuation", "oneroof_valuation_low",
              "oneroof_valuation_high", "oneroof_url"):
        assert hasattr(PropertyForSale, c), c


def test_no_two_portals_share_a_column():
    """The rule this broke. Two sources in one column means the column no
    longer means what its name says, and nothing on screen can tell you."""
    seen: dict[str, str] = {}
    for source, cols in ESTIMATE_COLUMNS.items():
        for c in cols:
            if not c:
                continue
            assert c not in seen, (
                f"{source} and {seen[c]} both write to {c} — whichever runs "
                f"last wins and the column stops meaning one portal")
            seen[c] = source


def test_every_estimate_column_is_real():
    """A mapping pointing at a column that does not exist fails at write time,
    inside a background job, where nobody sees it."""
    for source, (mid, low, high, url) in ESTIMATE_COLUMNS.items():
        for c in (mid, low, high, url):
            if c:
                assert hasattr(PropertyForSale, c), f"{source} -> {c}"


def test_the_oneroof_estimate_reaches_the_api():
    """Write-only columns are the same as no columns.

    This test used to name the four columns on the response model. That put
    the supplier's name on every row of the payload — a list of who we buy
    from, readable in the network tab — which the "nothing names the supplier"
    test caught by searching the serialised JSON.

    Deleting the fields was the wrong fix: an estimate we pay for, store and
    never publish is a lookup we wasted. So the NUMBER stays and the NAME
    goes. What this test enforces now is the number arriving, under a key that
    does not say where it came from.
    """
    from app.routers.properties import ForSaleRow

    for f in ("portal_a_valuation", "portal_a_valuation_low",
              "portal_a_valuation_high", "portal_a_url"):
        assert f in ForSaleRow.model_fields, f

    row = ForSaleRow.model_validate(_row(oneroof_valuation=1_900_000,
                                        oneroof_valuation_low=1_800_000,
                                        oneroof_valuation_high=2_000_000)).model_dump()
    assert row["portal_a_valuation"] == 1_900_000, (
        "the stored estimate never reaches the browser — a wasted lookup")
    assert row["portal_a_valuation_low"] == 1_800_000
    assert row["portal_a_valuation_high"] == 2_000_000


def test_the_portal_link_does_not_name_the_portal():
    """The key is neutral; the VALUE was still an address on their domain."""
    from app.routers.properties import ForSaleRow

    row = ForSaleRow.model_validate(
        _row(oneroof_url="https://www.oneroof.co.nz/p/12345")).model_dump()
    assert row["portal_a_url"].startswith("/api/go/")
    assert "oneroof" not in row["portal_a_url"].lower()


def test_a_stored_oneroof_estimate_does_not_touch_the_feed_figure(db_session):
    """The actual behaviour, not just the mapping."""
    from app.models import BatchType, ImportBatch
    from app.portals import PortalResult
    from app.portals.fill import apply

    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="live.xlsx", is_active=True, status="published")
    db_session.add(b)
    db_session.flush()
    p = PropertyForSale(import_batch_id=b.id, address="1 Test Rd",
                        suburb="Riverhead", cv_numeric=1_500_000,
                        third_party_valuation=1_400_000)
    db_session.add(p)
    db_session.commit()

    apply(p, PortalResult(source="oneroof", estimate=1_900_000))
    db_session.commit()

    assert p.third_party_valuation == 1_400_000, (
        "the weekly feed's Hougarden figure was overwritten again")
    assert p.oneroof_valuation == 1_900_000
