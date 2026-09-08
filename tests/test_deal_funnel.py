"""Where did the deals go?

    "the underpriced houses shows 9 that is bullshit"

A batch of 2,141 Auckland listings produced nine deals. Working back through the
exported data by hand, 126 of those rows satisfy every condition the deal rule
in pricing/scoring.py applies: a real asking price, a valuation at least 5% above
it, a margin the pipeline allowed to claim a deal, medium-or-high confidence, a
floor area, not held, not leasehold, not a duplicate unit in the same building.

Nine were flagged. That gap cannot be explained by reading the pricing code,
because the pricing code produced the 126 — the numbers sitting on those rows
are its output. The flag disagrees with the numbers stored beside it.

So the fix is an instrument, not an edit: count the batch after every gate, and
count the rows that clear all of them and are still not flagged. These tests are
about that instrument being right, because a diagnostic that lies is worse than
none — it sends you to rewrite the one part that was working.
"""
from __future__ import annotations

import pytest

from app.deal_funnel import deal_funnel
from app.models import (BatchType, ImportBatch, PropertyForSale, User, UserRole,
                        UserStatus)


def _batch(db, *, active=True):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="auckland.csv", is_active=active, status="published")
    db.add(b)
    db.commit()
    return b


def _row(db, batch, **kw):
    """A listing that IS a deal unless a keyword argument spoils it.

    is_underpriced is derived from the other fields rather than defaulted, so a
    spoiled row arrives the way a correctly-priced batch would write it: not a
    deal, and not flagged. Hard-coding it True would plant the exact fault these
    tests exist to detect in every fixture, and every test would find one.
    """
    d = dict(address="12 Elliot Street", suburb="Riverhead",
             property_type="House", type_of_title="Freehold",
             asking_price=1_000_000.0, cv_numeric=1_100_000.0,
             fair_value=1_200_000.0, margin=0.20, confidence="high",
             floor_area_m2=150.0, is_held=False, hold_reason=None)
    d.update(kw)
    d.setdefault("is_underpriced", bool(
        d["asking_price"] and d["fair_value"] and d["margin"] is not None
        and d["margin"] >= 0.05 and (d["confidence"] or "") in ("medium", "high")
        and d["type_of_title"] != "Leasehold" and not d["is_held"]))
    p = PropertyForSale(import_batch_id=batch.id, **d)
    db.add(p)
    db.commit()
    return p


# ---- the number that answers the question ----------------------------------
def test_a_row_that_passes_every_test_and_is_not_flagged_is_reported(db_session):
    """The whole point. Numbers say deal, flag says no — that is the finding."""
    b = _batch(db_session)
    _row(db_session, b, address="1 Real Deal Road", is_underpriced=True)
    _row(db_session, b, address="2 Lost Deal Road", is_underpriced=False)

    f = deal_funnel(db_session, b.id)
    assert f.mismatch == 1
    assert "2 Lost Deal Road" in f.mismatch_examples[0]
    assert f.flagged == 1


def test_a_clean_batch_reports_no_mismatch(db_session):
    """When the flag agrees with the data — which is the normal case — the
    diagnostic must say nothing is wrong. A funnel that always finds a problem
    is not a funnel."""
    b = _batch(db_session)
    for i in range(5):
        _row(db_session, b, address=f"{i} Fine Street")
    f = deal_funnel(db_session, b.id)
    assert f.mismatch == 0
    assert f.mismatch_examples == []
    assert f.flagged == 5


def test_the_examples_name_the_property_and_its_numbers(db_session):
    """An operator has to be able to open the listing and look at it. A count
    alone sends them back to the database."""
    b = _batch(db_session)
    _row(db_session, b, address="9 Proof Place", asking_price=800_000.0,
         fair_value=960_000.0, margin=0.20, is_underpriced=False)
    ex = deal_funnel(db_session, b.id).mismatch_examples[0]
    assert "9 Proof Place" in ex
    assert "800,000" in ex and "960,000" in ex
    assert "+20.0%" in ex
    assert "high confidence" in ex


def test_examples_are_capped_so_the_response_stays_readable(db_session):
    b = _batch(db_session)
    for i in range(30):
        _row(db_session, b, address=f"{i} Flood Street", is_underpriced=False)
    f = deal_funnel(db_session, b.id)
    assert f.mismatch == 30
    assert len(f.mismatch_examples) == 10


def test_examples_are_ordered_biggest_margin_first(db_session):
    """Fix the largest one first — it is the most visible and usually the most
    informative about what went wrong."""
    b = _batch(db_session)
    _row(db_session, b, address="small", margin=0.06, fair_value=1_060_000.0,
         is_underpriced=False)
    _row(db_session, b, address="huge", margin=0.40, fair_value=1_400_000.0,
         is_underpriced=False)
    assert "huge" in deal_funnel(db_session, b.id).mismatch_examples[0]


# ---- each gate has to subtract the rows it is supposed to subtract ---------
@pytest.mark.parametrize("label_fragment,spoil", [
    ("still shown", dict(is_held=True, hold_reason="Missing floor area")),
    ("advertised asking price", dict(asking_price=None)),
    ("produced a valuation", dict(fair_value=None)),
    ("counts as a deal signal", dict(margin=None)),
    ("more than the asking", dict(margin=0.01)),
    ("confident enough", dict(confidence="low")),
    ("not leasehold", dict(type_of_title="Leasehold")),
])
def test_each_gate_reports_the_row_it_removed(db_session, label_fragment, spoil):
    b = _batch(db_session)
    _row(db_session, b, address="1 Good Street")
    _row(db_session, b, address="2 Spoiled Street", **spoil)

    f = deal_funnel(db_session, b.id)
    step = next(s for s in f.steps if label_fragment in s.label)
    assert step.lost == 1, f"{step.label} did not account for the spoiled row"
    assert f.steps[-1].kept == 1


def test_every_step_explains_itself(db_session):
    """A count with no reason beside it gets read as a bug. Each gate says why
    it exists, in words an operator can act on."""
    b = _batch(db_session)
    _row(db_session, b)
    for s in deal_funnel(db_session, b.id).steps:
        assert s.why and len(s.why) > 10


def test_the_steps_only_ever_shrink(db_session):
    """It is a funnel. A step that grows means a gate is reading a different
    population from the one above it, which is how the tiles above a deal page
    ended up describing rows that were not underneath it."""
    b = _batch(db_session)
    for i in range(6):
        _row(db_session, b, address=f"{i} Order Street",
             **({"confidence": "low"} if i % 2 else {}))
    kept = [s.kept for s in deal_funnel(db_session, b.id).steps]
    assert kept == sorted(kept, reverse=True)


# ---- the held rows, by reason ----------------------------------------------
def test_hold_reasons_are_broken_out_biggest_first(db_session):
    """"1,377 held" is not actionable. "884 have no deal, 455 have no advertised
    price, 38 have a broken council record" is three different decisions."""
    b = _batch(db_session)
    for i in range(4):
        _row(db_session, b, is_held=True, hold_reason="Below $10,000 margin")
    for i in range(2):
        _row(db_session, b, is_held=True, hold_reason="Missing floor area")
    f = deal_funnel(db_session, b.id)
    assert f.hold_reasons[0] == ("Below $10,000 margin", 4)
    assert f.hold_reasons[1] == ("Missing floor area", 2)


def test_a_held_row_with_no_reason_still_gets_counted(db_session):
    """Silently dropping the ones with no reason recorded is exactly how a
    missing count stays missing."""
    b = _batch(db_session)
    _row(db_session, b, is_held=True, hold_reason=None)
    reasons = dict(deal_funnel(db_session, b.id).hold_reasons)
    assert sum(reasons.values()) == 1


# ---- the reverse fault: flagged when the data no longer supports it --------
def test_a_flag_the_numbers_no_longer_support_is_reported(db_session):
    """The opposite error, and the more dangerous one: it publishes a deal that
    is not there. A stale flag on a re-priced row looks identical to a real one
    on the deal page."""
    b = _batch(db_session)
    _row(db_session, b, address="stale flag", margin=0.01, is_underpriced=True)
    f = deal_funnel(db_session, b.id)
    assert f.orphan_flags == 1


# ---- it must describe the live batch, not a re-run -------------------------
def test_it_reads_stored_columns_and_never_reprices(db_session, monkeypatch):
    """If the diagnostic re-priced the batch it would report what a fresh run
    WOULD produce, which is the one thing that cannot explain a stale flag."""
    import app.pricing.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run",
                        lambda *a, **k: pytest.fail("the funnel re-priced the batch"))
    b = _batch(db_session)
    _row(db_session, b)
    assert deal_funnel(db_session, b.id).total == 1


def test_no_batch_is_an_empty_answer_not_a_crash(db_session):
    f = deal_funnel(db_session, None)
    assert f.total == 0 and f.mismatch == 0 and f.steps == []


def test_rows_from_another_batch_are_not_counted(db_session):
    """Two loads in the database at once is the normal state, not the exception."""
    live = _batch(db_session, active=True)
    other = _batch(db_session, active=False)
    _row(db_session, live)
    for i in range(7):
        _row(db_session, other, address=f"{i} Other Batch Street")
    assert deal_funnel(db_session, live.id).total == 1


# ---- through the endpoint --------------------------------------------------
@pytest.fixture()
def admin_client(db_session):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.security import current_user, require_active, require_admin

    admin = User(email="funnel-admin@test.local", password_hash="x",
                 role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(admin)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: admin,
        require_active: lambda: admin,
        require_admin: lambda: admin,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}


def test_endpoint_defaults_to_the_live_batch(admin_client, db_session):
    """The question is always about what customers are seeing right now, so the
    default has to be the batch they are seeing — not the staged one."""
    old = _batch(db_session, active=False)
    live = _batch(db_session, active=True)
    _row(db_session, old, address="old batch")
    _row(db_session, live, address="live batch", is_underpriced=False)

    r = admin_client.get("/api/admin/release/deal-funnel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == live.id
    assert body["total"] == 1
    assert body["mismatch"] == 1
    assert "live batch" in body["mismatch_examples"][0]


def test_endpoint_can_be_pointed_at_a_staged_batch(admin_client, db_session):
    live = _batch(db_session, active=True)
    staged = _batch(db_session, active=False)
    _row(db_session, live)
    for i in range(3):
        _row(db_session, staged, address=f"{i} Staged Street")

    r = admin_client.get(f"/api/admin/release/deal-funnel?batch_id={staged.id}")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 3


def test_endpoint_with_no_batch_at_all_answers_instead_of_erroring(admin_client):
    r = admin_client.get("/api/admin/release/deal-funnel")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0
