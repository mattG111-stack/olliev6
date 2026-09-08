"""The biggest number on the page was always the most broken row.

The runaway percentage — "+206.70%", "+2,296.9%" — was "vs CV", not the margin.
Worth stating plainly, because the two were confused once already and the fix
went to the wrong place first. Measured across 23 staged exports:

    vs CV %   n=229,900   median −1.5%   99th +123.9%   max +2,296.9%
    Margin %  n= 86,804   median +1.5%   99th  +23.4%   max    +79.9%

The vs-CV tail is land-only council records: median CV of $3,721 per m² of floor
against $8,000 for the median row. The worst is asking $80,000 against a
valuation of $1,917,500 on a CV of $80,000 — the dirt was valued and the house
standing on it never was. That is fixed at source in pricing.pipeline, which now
withholds the comparison instead of dividing by a CV it has already discarded
(see tests/test_pipeline_landonly.py).

What these tests cover is the publish gate behind it:

  * ONE rule for "is this CV only the land", instead of two that disagreed — the
    good one, and an inline copy using a $1 tolerance that let any record off by
    more than a dollar through with its deal signal intact
  * a margin CEILING to go with the long-standing floor. Zero rows in those 23
    exports are above it; it is a backstop for the next land-only CV that finds
    a route past the rule above, not a fix for anything visible today
  * both checked at publish AND at read, because batches published before either
    existed are still live
"""
from __future__ import annotations

import pytest

from app.models import PropertyForSale
from app.pricing.pipeline import is_land_only_cv
from app.release import (
    ABOVE_MARGIN_REASON,
    BELOW_MARGIN_REASON,
    LAND_ONLY_CV_REASON,
    MARGIN_MAX_PCT,
    _hold_reason,
)


def _row(**kw) -> PropertyForSale:
    """A clean, publishable listing. Each test breaks exactly one thing."""
    base = dict(
        address="1 Example Road", suburb="Papakura", district="Papakura",
        property_type="House", floor_area_m2=140.0, land_area_m2=600.0,
        asking_price=900_000.0, cv_numeric=880_000.0,
        land_value_numeric=520_000.0, improvement_value_numeric=360_000.0,
        fair_value=960_000.0, margin=960_000.0 / 900_000.0 - 1.0,
        land_area_flag=None, cv_flag=None, valuation_last_sold_value=None,
        expected_sale_path="listed",
    )
    base.update(kw)
    return PropertyForSale(**base)


# ---- the ceiling ------------------------------------------------------------
def test_the_clean_row_publishes():
    """Guard on the fixture itself: if this fails the other tests prove nothing."""
    assert _hold_reason(_row()) is None


def test_a_margin_above_the_ceiling_is_held():
    p = _row(fair_value=2_700_000.0, margin=2.0)
    assert _hold_reason(p) == ABOVE_MARGIN_REASON


def test_the_row_that_started_this_never_reaches_a_customer():
    """Asking $80,000, valued $1,917,500, CV $80,000 — the actual worst row."""
    p = _row(asking_price=80_000.0, cv_numeric=80_000.0,
             land_value_numeric=80_000.0, improvement_value_numeric=None,
             fair_value=1_917_500.0, margin=1_917_500.0 / 80_000.0 - 1.0)
    assert _hold_reason(p) is not None


def test_a_real_deal_still_publishes():
    """+22% sits inside the 99th percentile of real margins. Not a fault."""
    p = _row(asking_price=900_000.0, fair_value=1_100_000.0,
             margin=1_100_000.0 / 900_000.0 - 1.0)
    assert _hold_reason(p) is None


def test_the_ceiling_is_measured_against_asking_not_cv():
    """A high CV must not rescue a listing priced miles under its valuation."""
    p = _row(asking_price=500_000.0, cv_numeric=1_400_000.0,
             land_value_numeric=700_000.0, improvement_value_numeric=700_000.0,
             fair_value=1_400_000.0, margin=1.8)
    assert _hold_reason(p) == ABOVE_MARGIN_REASON


@pytest.mark.parametrize("mult, held", [
    (1.0 + MARGIN_MAX_PCT - 0.01, False),      # just under — a very good deal
    (1.0 + MARGIN_MAX_PCT + 0.01, True),       # just over — a data fault
])
def test_the_line_sits_where_it_says_it_does(mult, held):
    ask = 900_000.0
    p = _row(asking_price=ask, fair_value=ask * mult, margin=mult - 1.0)
    assert (_hold_reason(p) == ABOVE_MARGIN_REASON) is held


def test_a_row_with_no_asking_is_not_held_by_the_ceiling():
    """No asking means no margin to measure — a different check owns that row."""
    p = _row(asking_price=None, fair_value=960_000.0, margin=None)
    assert _hold_reason(p) != ABOVE_MARGIN_REASON


def test_a_data_fault_is_reported_ahead_of_no_deal():
    """Both checks fire on nothing; the message has to name the real problem."""
    p = _row(fair_value=3_000_000.0, margin=2.3)
    assert _hold_reason(p) == ABOVE_MARGIN_REASON
    assert _hold_reason(p) != BELOW_MARGIN_REASON


# ---- one land-only rule, not two -------------------------------------------
def test_a_land_only_cv_is_held():
    """CV equals the land value and no improvements — the house is unassessed."""
    p = _row(cv_numeric=610_000.0, land_value_numeric=610_000.0,
             improvement_value_numeric=None)
    assert _hold_reason(p) == LAND_ONLY_CV_REASON


def test_a_land_only_cv_that_misses_by_more_than_a_dollar_is_still_land_only():
    """The bug, exactly. The old inline rule used a $1 tolerance, so a council
    record off by $8,000 was read as a full valuation and kept its deal signal."""
    p = _row(cv_numeric=610_000.0, land_value_numeric=602_000.0,
             improvement_value_numeric=None)
    assert is_land_only_cv(610_000.0, 140.0, 600.0, None, 602_000.0)
    assert _hold_reason(p) == LAND_ONLY_CV_REASON


def test_a_valued_building_is_never_land_only():
    """An improvement value present is the most reliable signal there is."""
    p = _row(cv_numeric=880_000.0, land_value_numeric=520_000.0,
             improvement_value_numeric=360_000.0)
    assert _hold_reason(p) is None


def test_a_section_keeps_its_land_only_cv():
    """Bare land has no improvements because there is nothing to improve. That
    is a correct record, not a broken one — holding every section would empty
    the whole vacant-land category out of the feed."""
    p = _row(property_type="Section", floor_area_m2=None,
             cv_numeric=610_000.0, land_value_numeric=610_000.0,
             improvement_value_numeric=None)
    assert _hold_reason(p) != LAND_ONLY_CV_REASON


def test_the_pipeline_and_the_publish_gate_agree():
    """One fact, one function. Two rules that disagree is what shipped +2,297%."""
    from app import release

    cases = [
        (610_000.0, 140.0, 600.0, None, 610_000.0),      # exact
        (610_000.0, 140.0, 600.0, None, 602_000.0),      # off by $8k
        (880_000.0, 140.0, 600.0, 360_000.0, 520_000.0),  # building valued
        (240_000.0, 200.0, 600.0, None, None),            # no split, low $/m²
    ]
    for cv, floor, land, iv, lv in cases:
        p = _row(cv_numeric=cv, floor_area_m2=floor, land_area_m2=land,
                 improvement_value_numeric=iv, land_value_numeric=lv)
        assert release._cv_is_land_only(p) is is_land_only_cv(cv, floor, land, iv, lv)


# ---- the read-time gate, for batches published before the ceiling existed ----
def test_a_live_row_above_the_ceiling_is_hidden_from_the_feed(db_session):
    """The hold runs at publish. Batches already live were published without it,
    so the read path has to hide those rows too or the fix is not retroactive."""
    from app.models import BatchType, ImportBatch
    from app.routers.properties import _hide_bad_data

    batch = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                        filename="release-test.xlsx", is_active=True, status="published")
    db_session.add(batch)
    db_session.flush()

    ok = _row(address="2 Fine Street", margin=0.15)
    bad = _row(address="3 Broken Street", margin=2.0670)   # the +206.70% hero
    for r in (ok, bad):
        r.import_batch_id = batch.id
        r.is_held = False
        db_session.add(r)
    db_session.commit()

    q = _hide_bad_data(
        db_session.query(PropertyForSale)
        .filter(PropertyForSale.import_batch_id == batch.id))
    addresses = {r.address for r in q.all()}
    assert "2 Fine Street" in addresses
    assert "3 Broken Street" not in addresses, "the +206.70% row is still the hero"


def test_a_row_with_no_margin_still_shows(db_session):
    """A listing we could price but not compare is a listing, not a fault."""
    from app.models import BatchType, ImportBatch
    from app.routers.properties import _hide_bad_data

    batch = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                        filename="release-test.xlsx", is_active=True, status="published")
    db_session.add(batch)
    db_session.flush()

    r = _row(address="4 Quiet Lane", asking_price=None, margin=None)
    r.import_batch_id = batch.id
    r.is_held = False
    db_session.add(r)
    db_session.commit()

    q = _hide_bad_data(
        db_session.query(PropertyForSale)
        .filter(PropertyForSale.import_batch_id == batch.id))
    assert [x.address for x in q.all()] == ["4 Quiet Lane"]
