"""One button: ask the portals about this week's keepers.

After a release is priced there are a few dozen properties worth acting on. For
each one, Trade Me, OneRoof, realestate.co.nz, homes.co.nz and CoreLogic hold
things we do not — a floor area we are missing, a land area, a council
valuation, and each portal's own estimate.

The rules the whole feature rests on, and what these tests are for:

  A fact fills a field we are MISSING and never overwrites one we hold.
  An estimate is stored in that portal's own columns and never reaches a price.

The second is not caution for its own sake. Trade Me's published estimate for a
sold property turned out to be that property's own sale price indexed forward —
a median 1.2% from it across 54,692 sales. A system that fed that back in would
be confirming its own answers and calling it agreement.

No network is touched here. The portals are functions, so the job runs end to
end against fakes — which is also the only way to test the case that matters
most: a source that is down, lying, or returning a phone number where a floor
area should be.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import BatchType, ImportBatch, IngestJob, PropertyForSale
from app.portals import PortalResult
from app.portals.fill import apply
from app.portals.runner import candidates, run_portal_job


def _batch(db, *, n=3, held=0, **over):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="live.csv", rows_total=n, is_active=False,
                    status="staged")
    db.add(b); db.flush()
    for i in range(n):
        fields = dict(
            import_batch_id=b.id, region="Auckland", suburb="Mount Eden",
            address=f"{i + 1} Portal Street", asking_price=1_200_000,
            property_type="House", is_held=i < held,
        )
        fields.update(over)
        db.add(PropertyForSale(**fields))
    db.commit()
    return b


def _job(db, batch_id) -> int:
    """The same IngestJob row the upload and enrich stages use. Returns its id.

    An id rather than the object: the runner expunges and closes its session,
    which in these tests is the shared one, so an object held across the run is
    detached by the time the assertions read it.
    """
    from app.staged_stages import create_stage_job
    return create_stage_job(db, stage="portals", batch_id=batch_id,
                            region="Auckland", uploaded_by_id=None).id


def _answers(**by_source):
    """Fake portals: {"trademe": PortalResult(...)} → the lookups dict."""
    return {name: (lambda a, s, r=res: r) for name, res in by_source.items()}


# ---- what a portal may change ----------------------------------------------
def test_a_missing_field_is_filled(db_session):
    b = _batch(db_session, n=1, floor_area_m2=None)
    p = db_session.query(PropertyForSale).first()
    out = apply(p, PortalResult(source="oneroof", floor_area_m2=185.0))
    assert "floor_area_m2" in out.filled
    assert p.floor_area_m2 == 185.0


def test_a_field_we_already_hold_is_never_overwritten(db_session):
    """Ours came from a feed that also priced it. A second opinion is not better."""
    _batch(db_session, n=1, floor_area_m2=176.0)
    p = db_session.query(PropertyForSale).first()
    out = apply(p, PortalResult(source="oneroof", floor_area_m2=185.0))
    assert out.filled == []
    assert p.floor_area_m2 == 176.0


def test_their_estimate_is_stored_in_their_own_column(db_session):
    _batch(db_session, n=1)
    p = db_session.query(PropertyForSale).first()
    apply(p, PortalResult(source="trademe", estimate=1_240_000,
                          estimate_low=1_170_000, estimate_high=1_310_000))
    assert p.tm_valuation == 1_240_000
    assert p.tm_valuation_low == 1_170_000 and p.tm_valuation_high == 1_310_000


def test_two_portals_do_not_land_in_the_same_column(db_session):
    """Side by side is the point — a reader needs to see where they disagree."""
    _batch(db_session, n=1)
    p = db_session.query(PropertyForSale).first()
    apply(p, PortalResult(source="trademe", estimate=1_240_000))
    apply(p, PortalResult(source="homes", estimate=1_310_000))
    apply(p, PortalResult(source="oneroof", estimate=1_180_000))
    assert (p.tm_valuation, p.homes_valuation, p.third_party_valuation) == (
        1_240_000, 1_310_000, 1_180_000)


def test_no_portal_may_touch_our_own_numbers(db_session):
    """The rule the Trade Me export taught us. Nothing here reaches a price."""
    _batch(db_session, n=1, fair_value=1_150_000, buy_price=1_090_000,
           asking_price=1_200_000, cv_numeric=1_100_000)
    p = db_session.query(PropertyForSale).first()
    apply(p, PortalResult(source="trademe", estimate=1_900_000,
                          cv_numeric=1_500_000))
    assert p.fair_value == 1_150_000, "a portal estimate reached our valuation"
    assert p.buy_price == 1_090_000, "a portal estimate reached the buy price"
    assert p.asking_price == 1_200_000
    assert p.cv_numeric == 1_100_000, "a portal overwrote a council valuation we hold"


def test_a_figure_that_cannot_be_a_floor_area_is_ignored(db_session):
    """Actors are written by other people against pages that change.

    A listing id, a page number or a phone number arriving in the floor-area
    field must not become a priced attribute.
    """
    _batch(db_session, n=1, floor_area_m2=None, land_area_m2=None)
    p = db_session.query(PropertyForSale).first()
    apply(p, PortalResult(source="trademe", floor_area_m2=0.4,
                          land_area_m2=9_000_000.0))
    assert p.floor_area_m2 is None and p.land_area_m2 is None


def test_an_absurd_estimate_is_not_stored(db_session):
    _batch(db_session, n=1)
    p = db_session.query(PropertyForSale).first()
    apply(p, PortalResult(source="trademe", estimate=12.0))
    assert p.tm_valuation is None


def test_a_dry_run_reports_without_writing(db_session):
    _batch(db_session, n=1, floor_area_m2=None)
    p = db_session.query(PropertyForSale).first()
    out = apply(p, PortalResult(source="oneroof", floor_area_m2=185.0),
                dry_run=True)
    assert out.filled == ["floor_area_m2"] and p.floor_area_m2 is None


# ---- who gets asked ---------------------------------------------------------
def test_only_the_keepers_are_asked_about(db_session):
    """A weekly file is thousands of listings; the deals are a few dozen."""
    b = _batch(db_session, n=5, held=3)
    assert len(candidates(db_session, b.id)) == 2


def test_a_property_asked_about_recently_is_skipped(db_session):
    """So pressing the button twice costs nothing and a dead run resumes."""
    b = _batch(db_session, n=2)
    p = db_session.query(PropertyForSale).first()
    p.portals_checked_at = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.commit()
    assert len(candidates(db_session, b.id)) == 1


def test_but_it_is_asked_again_once_the_answer_is_old(db_session):
    b = _batch(db_session, n=1)
    p = db_session.query(PropertyForSale).first()
    p.portals_checked_at = datetime.now(timezone.utc) - timedelta(days=90)
    db_session.commit()
    assert len(candidates(db_session, b.id)) == 1


def test_a_row_with_no_address_is_not_asked_about(db_session):
    b = _batch(db_session, n=2)
    p = db_session.query(PropertyForSale).first()
    p.address = None
    db_session.commit()
    assert len(candidates(db_session, b.id)) == 1


# ---- the job ----------------------------------------------------------------
def test_the_job_fills_from_several_portals_at_once(db_session, monkeypatch):
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    b = _batch(db_session, n=2, floor_area_m2=None, land_area_m2=None)
    job_id = _job(db_session, b.id)

    run_portal_job(job_id, b.id, delay=0, review=False, lookups=_answers(
        oneroof=PortalResult(source="oneroof", floor_area_m2=185.0,
                             estimate=1_180_000),
        trademe=PortalResult(source="trademe", land_area_m2=620.0,
                             estimate=1_240_000),
    ), sources=("oneroof", "trademe"))

    db_session.expire_all()
    for p in db_session.query(PropertyForSale).all():
        assert p.floor_area_m2 == 185.0 and p.land_area_m2 == 620.0
        assert p.third_party_valuation == 1_180_000 and p.tm_valuation == 1_240_000
        assert p.portals_checked_at is not None

    job = db_session.get(IngestJob, job_id)
    db_session.refresh(job)
    assert job.status == "completed" and job.progress_pct == 100
    assert "2 of 2 properties answered" in json.loads(job.result_json)["summary"]


def test_one_portal_being_down_does_not_cost_the_others(db_session, monkeypatch):
    """The case that decides whether this survives a real week."""
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    b = _batch(db_session, n=1, floor_area_m2=None)
    job_id = _job(db_session, b.id)

    def broken(address, suburb):
        raise RuntimeError("503 from the portal")

    run_portal_job(job_id, b.id, delay=0, review=False, sources=("trademe", "oneroof"),
                   lookups={"trademe": broken,
                            **_answers(oneroof=PortalResult(
                                source="oneroof", floor_area_m2=185.0))})

    db_session.expire_all()
    assert db_session.query(PropertyForSale).first().floor_area_m2 == 185.0
    job = db_session.get(IngestJob, job_id)
    db_session.refresh(job)
    assert job.status == "completed", job.error_message


def test_a_property_no_portal_knows_is_not_asked_about_forever(db_session,
                                                               monkeypatch):
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    b = _batch(db_session, n=1)
    batch_id = b.id                     # the runner expunges; hold the number
    job_id = _job(db_session, batch_id)

    run_portal_job(job_id, batch_id, delay=0, sources=("trademe",),
                   lookups={"trademe": lambda a, s: None})

    db_session.expire_all()
    p = db_session.query(PropertyForSale).first()
    assert p.portals_checked_at is not None, (
        "an address nobody recognises would be re-asked of five sources every run"
    )
    assert candidates(db_session, batch_id) == []


def test_nothing_to_do_finishes_rather_than_hanging(db_session, monkeypatch):
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    b = _batch(db_session, n=1, held=1)
    job_id = _job(db_session, b.id)
    run_portal_job(job_id, b.id, delay=0, lookups={})
    job = db_session.get(IngestJob, job_id)
    db_session.refresh(job)
    assert job.status == "completed" and job.progress_pct == 100


def test_the_cap_is_respected(db_session, monkeypatch):
    """A mis-click must not spend an afternoon and a fortune."""
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    b = _batch(db_session, n=6, floor_area_m2=None)
    job_id = _job(db_session, b.id)
    run_portal_job(job_id, b.id, delay=0, review=False, cap=2, sources=("oneroof",),
                   lookups=_answers(oneroof=PortalResult(
                       source="oneroof", floor_area_m2=185.0)))
    db_session.expire_all()
    done = [p for p in db_session.query(PropertyForSale).all()
            if p.floor_area_m2 is not None]
    assert len(done) == 2


def test_progress_is_reported_while_it_runs(db_session, monkeypatch):
    """The button needs a progress bar, and it reads the same IngestJob row the
    upload and enrich stages use."""
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    b = _batch(db_session, n=5, floor_area_m2=None)
    job_id = _job(db_session, b.id)
    run_portal_job(job_id, b.id, delay=0, sources=("oneroof",),
                   lookups=_answers(oneroof=PortalResult(
                       source="oneroof", floor_area_m2=185.0)))
    job = db_session.get(IngestJob, job_id)
    db_session.refresh(job)
    assert job.rows_total == 5 and job.rows_inserted == 5
    assert job.rows_filled and job.rows_filled >= 5


# ---- the cleaning rules still apply -----------------------------------------
def test_a_filled_floor_area_sends_the_listing_back_through_pricing(db_session,
                                                                    monkeypatch):
    """A portal must not be able to move a listing across the margin floor
    without the floor being re-checked.

    Filling a floor area changes what the property is worth. If the stored value
    were left as it was, the number on screen would be from before the fill, and
    the hold decision would have been made on the old one.
    """
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    seen: list[int] = []
    monkeypatch.setattr("app.portals.runner.reprice_one",
                        lambda db, pid, **kw: seen.append(pid))

    b = _batch(db_session, n=1, floor_area_m2=None)
    batch_id = b.id
    job_id = _job(db_session, batch_id)
    run_portal_job(job_id, batch_id, delay=0, review=False, sources=("oneroof",),
                   lookups=_answers(oneroof=PortalResult(
                       source="oneroof", floor_area_m2=185.0)))

    assert seen, "a filled floor area did not trigger a re-price"


def test_an_estimate_alone_does_not_trigger_a_re_price(db_session, monkeypatch):
    """Their opinion is not our input, so nothing about our price has changed."""
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    seen: list[int] = []
    monkeypatch.setattr("app.portals.runner.reprice_one",
                        lambda db, pid, **kw: seen.append(pid))

    b = _batch(db_session, n=1, floor_area_m2=140.0)
    batch_id = b.id
    job_id = _job(db_session, batch_id)
    run_portal_job(job_id, batch_id, delay=0, review=False, sources=("trademe",),
                   lookups=_answers(trademe=PortalResult(
                       source="trademe", estimate=1_240_000)))

    assert seen == [], "storing a portal's estimate re-priced the listing"


def test_a_photo_does_not_trigger_a_re_price(db_session, monkeypatch):
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    seen: list[int] = []
    monkeypatch.setattr("app.portals.runner.reprice_one",
                        lambda db, pid, **kw: seen.append(pid))

    b = _batch(db_session, n=1, floor_area_m2=140.0, image_url=None)
    batch_id = b.id
    job_id = _job(db_session, batch_id)
    run_portal_job(job_id, batch_id, delay=0, review=False, sources=("oneroof",),
                   lookups=_answers(oneroof=PortalResult(
                       source="oneroof", image_url="https://example.invalid/a.jpg")))
    assert seen == []


def test_a_re_price_that_cannot_run_does_not_fail_the_job(db_session, monkeypatch):
    """No sold data to price against yet is a normal state, not a failure."""
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)

    def no_sold(db, pid, **kw):
        raise ValueError("no sold batch to price against")

    monkeypatch.setattr("app.portals.runner.reprice_one", no_sold)
    b = _batch(db_session, n=1, floor_area_m2=None)
    batch_id = b.id
    job_id = _job(db_session, batch_id)
    run_portal_job(job_id, batch_id, delay=0, review=False, sources=("oneroof",),
                   lookups=_answers(oneroof=PortalResult(
                       source="oneroof", floor_area_m2=185.0)))
    job = db_session.get(IngestJob, job_id)
    db_session.refresh(job)
    assert job.status == "completed", job.error_message


# ---- the daily pass ----------------------------------------------------------
def test_the_daily_pass_is_off_unless_switched_on(monkeypatch):
    """A job that reaches the internet and spends money is opt-in."""
    from app.portals import daily

    monkeypatch.setattr(daily.settings, "portals_daily", False, raising=False)
    assert daily.enabled() is False
    daily.start()                                  # must be a no-op, not a thread


def test_the_daily_pass_runs_over_the_live_batch(db_session, monkeypatch):
    """The live one is what customers are looking at."""
    from app.portals import daily

    monkeypatch.setattr(daily, "SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)

    old = _batch(db_session, n=1, floor_area_m2=None)
    old.is_active = False
    live = _batch(db_session, n=1, floor_area_m2=None)
    live.is_active = True
    db_session.commit()
    live_id = live.id

    monkeypatch.setattr("app.portals.runner.LOOKUPS", {})
    jid = daily.run_once(cap=5)
    assert jid is not None
    job = db_session.get(IngestJob, jid)
    assert job.batch_id == live_id, "the daily pass ran over a batch nobody can see"


def test_the_daily_pass_with_no_live_batch_does_nothing(db_session, monkeypatch):
    from app.portals import daily

    monkeypatch.setattr(daily, "SessionLocal", lambda: db_session)
    assert daily.run_once() is None


# ---- nothing is written until someone says so -------------------------------
def test_the_pass_changes_nothing_by_itself(db_session, monkeypatch):
    """The default. A figure scraped off someone else's page is a claim."""
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    from app.models import PortalFinding

    b = _batch(db_session, n=1, floor_area_m2=None)
    batch_id = b.id
    job_id = _job(db_session, batch_id)
    run_portal_job(job_id, batch_id, delay=0, sources=("oneroof",),
                   lookups=_answers(oneroof=PortalResult(
                       source="oneroof", floor_area_m2=185.0, estimate=1_180_000)))

    db_session.expire_all()
    p = db_session.query(PropertyForSale).first()
    assert p.floor_area_m2 is None, "a portal wrote to a listing without being asked"
    assert p.third_party_valuation is None

    pending = db_session.query(PortalFinding).filter(
        PortalFinding.status == "pending").all()
    assert {f.field for f in pending} == {"floor_area_m2", "third_party_valuation"}
    assert all(f.source == "oneroof" for f in pending)


def test_a_finding_carries_what_we_hold_now(db_session, monkeypatch):
    """So a reader can see what they are choosing between."""
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    from app.models import PortalFinding

    b = _batch(db_session, n=1, floor_area_m2=None)
    batch_id = b.id
    run_portal_job(_job(db_session, batch_id), batch_id, delay=0,
                   sources=("oneroof",),
                   lookups=_answers(oneroof=PortalResult(
                       source="oneroof", floor_area_m2=185.0)))
    f = db_session.query(PortalFinding).filter(
        PortalFinding.field == "floor_area_m2").one()
    assert f.value_num == 185.0
    assert f.current_num is None            # which is why it was offered
    assert f.kind == "fact"                 # it changes what the place is worth


def test_approving_writes_it_and_re_prices(db_session, monkeypatch):
    from app.models import PortalFinding
    from app.portals.findings import approve

    _batch(db_session, n=1, floor_area_m2=None)
    p = db_session.query(PropertyForSale).first()
    f = PortalFinding(property_id=p.id, source="oneroof", field="floor_area_m2",
                      kind="fact", value_num=185.0, status="pending")
    db_session.add(f); db_session.commit()

    seen: list[int] = []
    ok, why = approve(db_session, f.id, reprice=lambda db, pid: seen.append(pid))
    assert ok, why
    db_session.refresh(p)
    assert p.floor_area_m2 == 185.0
    assert seen == [p.id], "an approved fact did not go back through pricing"
    db_session.refresh(f)
    assert f.status == "applied"


def test_approving_an_estimate_does_not_re_price(db_session):
    from app.models import PortalFinding
    from app.portals.findings import approve

    _batch(db_session, n=1)
    p = db_session.query(PropertyForSale).first()
    f = PortalFinding(property_id=p.id, source="trademe", field="tm_valuation",
                      kind="estimate", value_num=1_240_000, status="pending",
                      extra_json=json.dumps({"low": 1_170_000, "high": 1_310_000,
                                             "low_col": "tm_valuation_low",
                                             "high_col": "tm_valuation_high"}))
    db_session.add(f); db_session.commit()

    seen: list[int] = []
    ok, _ = approve(db_session, f.id, reprice=lambda db, pid: seen.append(pid))
    assert ok
    db_session.refresh(p)
    assert p.tm_valuation == 1_240_000 and p.tm_valuation_low == 1_170_000
    assert seen == [], "storing a portal's opinion re-priced the listing"


def test_rejecting_writes_nothing_and_is_remembered(db_session):
    """Next week the same portal offers the same wrong number."""
    from app.models import PortalFinding
    from app.portals.findings import reject

    _batch(db_session, n=1, floor_area_m2=None)
    p = db_session.query(PropertyForSale).first()
    f = PortalFinding(property_id=p.id, source="trademe", field="floor_area_m2",
                      kind="fact", value_num=185.0, status="pending")
    db_session.add(f); db_session.commit()

    assert reject(db_session, f.id) is True
    db_session.refresh(p); db_session.refresh(f)
    assert p.floor_area_m2 is None
    assert f.status == "rejected", "a refusal has to be kept, or it is made again"


def test_a_refused_number_is_not_offered_again(db_session, monkeypatch):
    monkeypatch.setattr("app.portals.runner.SessionLocal", lambda: db_session)
    from app.models import PortalFinding
    from app.portals.findings import reject

    b = _batch(db_session, n=1, floor_area_m2=None)
    batch_id = b.id
    answer = _answers(oneroof=PortalResult(source="oneroof", floor_area_m2=185.0))

    run_portal_job(_job(db_session, batch_id), batch_id, delay=0,
                   sources=("oneroof",), lookups=answer)
    f = db_session.query(PortalFinding).one()
    reject(db_session, f.id)

    # A fortnight later, same answer from the same portal.
    p = db_session.query(PropertyForSale).first()
    p.portals_checked_at = None
    db_session.commit()
    run_portal_job(_job(db_session, batch_id), batch_id, delay=0,
                   sources=("oneroof",), lookups=answer)

    assert db_session.query(PortalFinding).count() == 1, (
        "a number already refused came back as a new decision to make"
    )


def test_a_value_that_arrived_in_the_meantime_wins(db_session):
    """Between the lookup and the decision, the real data may turn up."""
    from app.models import PortalFinding
    from app.portals.findings import approve

    _batch(db_session, n=1, floor_area_m2=None)
    p = db_session.query(PropertyForSale).first()
    f = PortalFinding(property_id=p.id, source="trademe", field="floor_area_m2",
                      kind="fact", value_num=185.0, status="pending")
    db_session.add(f); db_session.commit()

    p.floor_area_m2 = 176.0                 # our own feed, after the lookup
    db_session.commit()

    ok, why = approve(db_session, f.id)
    assert not ok and "our own value" in why
    db_session.refresh(p)
    assert p.floor_area_m2 == 176.0


def test_the_same_finding_cannot_be_applied_twice(db_session):
    from app.models import PortalFinding
    from app.portals.findings import approve

    _batch(db_session, n=1, floor_area_m2=None)
    p = db_session.query(PropertyForSale).first()
    f = PortalFinding(property_id=p.id, source="oneroof", field="floor_area_m2",
                      kind="fact", value_num=185.0, status="pending")
    db_session.add(f); db_session.commit()

    assert approve(db_session, f.id, reprice=lambda db, pid: None)[0] is True
    ok, why = approve(db_session, f.id, reprice=lambda db, pid: None)
    assert not ok and "already" in why
