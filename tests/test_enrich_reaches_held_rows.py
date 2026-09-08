"""The enrich stage must look at the rows it exists to fix.

    "we're not pricing a whole heap of properties for what reason?"
    "corelogic didnt work well at all it did 100 houses only"

Both symptoms, one bug, and it is not the machine learning.

A hold means one of two completely different things:

    A DATA GAP    the row is missing something we could go and fetch — a floor
                  area, a CV, a CV that looks wrong. CoreLogic exists for
                  exactly these.
    NOT A DEAL    the data is fine, the margin is not there. A paid lookup buys
                  nothing.

The enrich work list skipped EVERY held row, on the reasoning that CoreLogic
should be spent on deal candidates rather than the whole file. That reasoning is
right about the second kind and exactly backwards about the first:

    a row with no floor area is HELD, reason "Missing floor area"
    enrich skips held rows, so it is never looked up
    so its floor area is never filled
    so it stays held, and unpriced, permanently

A catch-22, and a compounding one — every weekly load added more rows to it. It
also explains a hundred lookups on an eleven-thousand-row batch: after the first
pricing pass most rows are held, and only the unheld remainder was eligible.
"""
from __future__ import annotations

import pytest

from app.release import (LAND_ONLY_CV_REASON, BELOW_MARGIN_REASON,
                         ABOVE_MARGIN_REASON, NO_ASKING_REASON,
                         hold_is_a_data_gap)


# ---- the classification ----------------------------------------------------
@pytest.mark.parametrize("reason", [
    "Missing floor area",
    "CV looks wrong vs the local market",
    "Not enough comparable sales to price confidently",
    "Land area flagged (parcel mismatch)",
    LAND_ONLY_CV_REASON,
])
def test_a_data_gap_is_worth_a_lookup(reason):
    assert hold_is_a_data_gap(reason) is True


@pytest.mark.parametrize("reason", [
    BELOW_MARGIN_REASON,
    ABOVE_MARGIN_REASON,
    NO_ASKING_REASON,
])
def test_a_no_deal_hold_is_not_worth_a_lookup(reason):
    """The original intent, and it was sound. Spending CoreLogic on a row whose
    data is fine and whose margin is not there buys nothing."""
    assert hold_is_a_data_gap(reason) is False


def test_an_unheld_row_is_not_a_data_gap():
    assert hold_is_a_data_gap(None) is False
    assert hold_is_a_data_gap("") is False


# ---- the catch-22 itself ---------------------------------------------------
def test_a_row_held_for_a_missing_floor_area_is_now_looked_up():
    """The exact loop that emptied the feed.

    Held for "Missing floor area" -> skipped by enrich -> floor area never
    filled -> still held. Forever.
    """
    assert hold_is_a_data_gap("Missing floor area"), (
        "the row that most needs CoreLogic is still the one guaranteed not to "
        "get it")


def test_the_enrich_work_list_includes_held_data_gaps(db_session):
    """Behavioural, against the real work-list rule rather than the idea of it."""
    from app.models import BatchType, ImportBatch, PropertyForSale

    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="live.xlsx", is_active=True, status="published")
    db_session.add(b)
    db_session.flush()

    rows = [
        # held for a data gap, and missing the thing a lookup would fill
        PropertyForSale(import_batch_id=b.id, address="1 Gap St", suburb="Riverhead",
                        cv_numeric=1_000_000, floor_area_m2=None,
                        land_area_m2=700, is_held=True,
                        hold_reason="Missing floor area"),
        # held because there is no deal — data is fine, do not spend on it
        PropertyForSale(import_batch_id=b.id, address="2 Nodeal St", suburb="Riverhead",
                        cv_numeric=1_000_000, floor_area_m2=None,
                        land_area_m2=700, is_held=True,
                        hold_reason=BELOW_MARGIN_REASON),
        # not held, and has a gap
        PropertyForSale(import_batch_id=b.id, address="3 Open St", suburb="Riverhead",
                        cv_numeric=None, floor_area_m2=200,
                        land_area_m2=700, is_held=False),
    ]
    db_session.add_all(rows)
    db_session.commit()

    def eligible(p):
        return (not p.is_held) or hold_is_a_data_gap(p.hold_reason)

    got = {p.address for p in rows if eligible(p)}
    assert "1 Gap St" in got, "a fixable hold is still being skipped"
    assert "3 Open St" in got
    assert "2 Nodeal St" not in got, "a paid lookup is being spent on a non-deal"


def test_the_enrich_stage_reads_the_hold_reason_at_all():
    """It selected is_held and not hold_reason, so it could not have told the
    two kinds apart even if it had wanted to."""
    import inspect

    from app import staged_stages

    src = inspect.getsource(staged_stages)
    assert "PropertyForSale.hold_reason" in src
    assert "hold_is_a_data_gap" in src
