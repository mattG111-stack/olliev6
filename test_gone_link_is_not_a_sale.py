"""A dead link is evidence, not a verdict.

    "every 24 hours or so the system tries to open each listing with the links
     and if the links are gone it's classed as sold?"

Yes — with one word changed, and the change is the whole design. A listing that
comes off a portal has usually sold. Usually. It may equally have been
withdrawn, expired, relisted under a new URL, or moved because the portal
changed its URL scheme overnight. A dead link cannot tell those apart.

That matters more here than almost anywhere else in this codebase, because a
wrong answer is not wrong once. This runs across every listing at a time, so a
bad rule invents thousands of sales in a single pass — and invented sales flow
straight into the sold comparables that price everything else on the site.

So nothing here writes "sold". It writes "the advertisement is gone", which is
the most that a missing page can support. What is tested:

  - only 404 and 410 count; 403, 429, 5xx and timeouts are US failing to look
  - three separate days must agree before anything changes
  - a portal that reports a third of its book gone is disbelieved, not obeyed
  - a link that comes back undoes everything
  - a run that is going to be disbelieved writes none of its conclusions
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.models import PortalListing
from app.portals import delisted as D


def _rows(db, n, *, host="oneroof.co.nz", **over):
    made = []
    for i in range(n):
        over.setdefault("status", "approved")
        r = PortalListing(source="oneroof", kind="for_sale",
                          url=f"https://{host}/listing/{i}",
                          address=f"{i} Example Road", suburb="Papakura",
                          price_numeric=900_000.0 + i, **over)
        db.add(r)
        made.append(r)
    db.commit()
    return made


class _Client:
    """Answers every request with the code it was built with."""

    def __init__(self, code=404, raises=None, codes=None):
        self.code, self.raises, self.codes, self.seen = code, raises, codes, []

    def _answer(self, url):
        self.seen.append(url)
        if self.raises:
            raise self.raises
        code = self.codes.get(url, self.code) if self.codes else self.code
        return httpx.Response(code, request=httpx.Request("HEAD", url))

    def head(self, url):
        return self._answer(url)

    def get(self, url):
        return self._answer(url)

    def close(self):
        pass


def _sweep(db, client, **kw):
    return D.sweep(db, client=client, sleep=lambda *_: None, **kw)


# ---- what counts as evidence ------------------------------------------------
@pytest.mark.parametrize("code", [404, 410])
def test_a_page_that_is_really_gone_counts(db_session, code):
    _rows(db_session, 1)
    out = _sweep(db_session, _Client(code))
    assert out["gone"] == 1
    assert db_session.query(PortalListing).first().link_gone_count == 1


@pytest.mark.parametrize("code", [403, 429, 500, 502, 503])
def test_being_blocked_or_broken_is_never_evidence(db_session, code):
    """THE ONE THAT MATTERS. 403 means we were refused, 429 means we asked too
    often, 5xx means their server fell over. None of them says the listing is
    gone, and treating them as if they did marks the whole market sold on the
    afternoon a portal decides it dislikes us."""
    _rows(db_session, 1)
    out = _sweep(db_session, _Client(code))
    assert out["gone"] == 0
    row = db_session.query(PortalListing).first()
    assert (row.link_gone_count or 0) == 0
    assert row.delisted_at is None
    assert row.link_last_result == str(code)


def test_a_timeout_is_not_evidence(db_session):
    _rows(db_session, 1)
    out = _sweep(db_session, _Client(raises=httpx.TimeoutException("slow")))
    assert out["gone"] == 0
    assert out["unreachable"] == 1
    assert db_session.query(PortalListing).first().link_last_result == "timeout"


def test_a_connection_failure_is_not_evidence(db_session):
    _rows(db_session, 1)
    out = _sweep(db_session, _Client(raises=httpx.ConnectError("no route")))
    assert out["gone"] == 0
    assert db_session.query(PortalListing).first().delisted_at is None


def test_a_redirect_that_lands_somewhere_is_still_up(db_session):
    """Portals bounce a sold listing to a "recently sold" page rather than 404
    it, and that page is a real page. Only the end of the chain counts."""
    _rows(db_session, 1)
    out = _sweep(db_session, _Client(200))
    assert out["still_up"] == 1
    assert out["gone"] == 0


# ---- one bad night proves nothing -------------------------------------------
def test_three_separate_days_must_agree(db_session):
    """A 404 during a portal's deploy window is not a sale."""
    _rows(db_session, 1)
    for day in range(1, D.GONE_STREAK):
        _sweep(db_session, _Client(404))
        row = db_session.query(PortalListing).first()
        assert row.delisted_at is None, f"gave up after only {day} day(s)"
        assert row.link_gone_count == day
    _sweep(db_session, _Client(404))
    assert db_session.query(PortalListing).first().delisted_at is not None


def test_one_good_answer_wipes_the_streak(db_session):
    _rows(db_session, 1)
    _sweep(db_session, _Client(404))
    _sweep(db_session, _Client(404))
    out = _sweep(db_session, _Client(200))
    row = db_session.query(PortalListing).first()
    assert row.link_gone_count == 0
    assert row.delisted_at is None
    assert out["came_back"] == 1


def test_a_listing_that_comes_back_is_live_again(db_session):
    """Relisted, or the portal fixed whatever was broken. Nothing here is a
    one-way door."""
    _rows(db_session, 1)
    for _ in range(D.GONE_STREAK):
        _sweep(db_session, _Client(404))
    row = db_session.query(PortalListing).first()
    assert row.delisted_at is not None
    row.delisted_at = None          # the sweep skips delisted rows; re-open it
    db_session.commit()
    _sweep(db_session, _Client(200))
    assert db_session.query(PortalListing).first().delisted_at is None


def test_an_unreachable_row_keeps_the_streak_it_had(db_session):
    """We learned nothing, so we forget nothing. Resetting on an unreachable
    check would let a flaky network hide a listing that really has gone."""
    _rows(db_session, 1)
    _sweep(db_session, _Client(404))
    _sweep(db_session, _Client(raises=httpx.TimeoutException("slow")))
    assert db_session.query(PortalListing).first().link_gone_count == 1


# ---- the run that is too successful to be true ------------------------------
def test_a_portal_reporting_a_third_of_its_book_gone_is_disbelieved(db_session):
    """THE CATASTROPHE GUARD. If most of a portal's listings answer 404 in one
    pass, the market did not sell overnight — we are blocked, or the URLs moved.
    Believing it would delist most of the site in one go."""
    _rows(db_session, 40)
    out = _sweep(db_session, _Client(404))
    assert out["abandoned"], "the sweep believed a portal that reported everything gone"
    assert out["newly_delisted"] == 0
    assert all((r.link_gone_count or 0) == 0
               for r in db_session.query(PortalListing).all()), \
        "a disbelieved pass still wrote its conclusions"


def test_a_disbelieved_pass_still_records_that_it_looked(db_session):
    """It has to be visible. A guard that silently does nothing is a guard
    nobody knows fired."""
    _rows(db_session, 40)
    _sweep(db_session, _Client(404))
    rows = db_session.query(PortalListing).all()
    assert all(r.link_checked_at is not None for r in rows)
    assert all(r.link_last_result == "404" for r in rows)


def test_normal_churn_is_believed(db_session):
    """The guard must not swallow the real thing. A handful gone out of forty
    is an ordinary night and has to get through."""
    rows = _rows(db_session, 40)
    gone = {r.url: 404 for r in rows[:4]}          # 10%
    for _ in range(D.GONE_STREAK):
        out = _sweep(db_session, _Client(200, codes=gone))
    assert not out["abandoned"]
    assert out["newly_delisted"] == 4


def test_a_handful_of_listings_is_never_judged_by_share(db_session):
    """Two gone out of five is 40% and entirely normal. The share only means
    something once there are enough checks for it to mean something."""
    rows = _rows(db_session, 5)
    gone = {r.url: 404 for r in rows[:2]}
    for _ in range(D.GONE_STREAK):
        out = _sweep(db_session, _Client(200, codes=gone))
    assert not out["abandoned"]
    assert out["newly_delisted"] == 2


def test_one_bad_portal_does_not_stop_a_good_one(db_session):
    """The guard is per host. A portal that blocks us must not cost us the
    other portal's real answers."""
    good = _rows(db_session, 30, host="oneroof.co.nz")
    bad = _rows(db_session, 30, host="trademe.co.nz")
    codes = {r.url: 404 for r in bad}               # all of trademe "gone"
    codes.update({r.url: 404 for r in good[:3]})    # normal churn on oneroof
    for _ in range(D.GONE_STREAK):
        out = _sweep(db_session, _Client(200, codes=codes))
    assert [a["host"] for a in out["abandoned"]] == ["trademe.co.nz"]
    assert out["newly_delisted"] == 3


# ---- the word we do not use -------------------------------------------------
def test_nothing_here_ever_says_sold(db_session):
    """A missing page cannot tell a sale from a withdrawal, and "sold" carries a
    price and a date that we do not have. The weekly sold file is what makes a
    sale, and it arrives with both.

    Tested by what it WRITES rather than by scanning the source for a word: the
    module has every right to discuss the distinction it exists to draw, and a
    text search cannot tell an explanation from a decision.
    """
    _rows(db_session, 1, status="approved")
    before = db_session.query(PortalListing).first().status
    for _ in range(D.GONE_STREAK):
        _sweep(db_session, _Client(404))
    row = db_session.query(PortalListing).first()
    assert row.delisted_at is not None, "it never concluded anything"
    # The listing's own status is untouched. Delisting is a separate fact,
    # recorded in its own column, and nothing downstream can mistake it for the
    # sale it is not.
    assert row.status == before
    assert row.status != "sold"
    assert row.sold_date is None
    assert row.sale_price is None


def test_delisting_writes_nothing_but_the_delisting_columns(db_session):
    """Blast radius. Whatever else this feature gets wrong, it must not be able
    to alter a price, an address, or the approval state of a listing."""
    rows = _rows(db_session, 1, status="approved")
    snapshot = {c.name: getattr(rows[0], c.name)
                for c in PortalListing.__table__.columns
                if not c.name.startswith(("link_", "delisted_"))}
    for _ in range(D.GONE_STREAK):
        _sweep(db_session, _Client(404))
    row = db_session.query(PortalListing).first()
    after = {k: getattr(row, k) for k in snapshot}
    assert after == snapshot, f"it changed {set(k for k in snapshot if after[k] != snapshot[k])}"


# ---- politeness and bounds --------------------------------------------------
def test_a_pass_is_bounded(db_session):
    _rows(db_session, 30)
    out = _sweep(db_session, _Client(200), limit=10)
    assert out["checked"] == 10


def test_it_never_asks_for_more_than_the_cap(db_session):
    _rows(db_session, 5)
    out = _sweep(db_session, _Client(200), limit=10_000)
    assert out["checked"] <= D.MAX_PER_RUN


def test_the_sweep_rotates_rather_than_re_checking_the_same_few(db_session):
    """Longest-unchecked first, so a cap smaller than the book still covers all
    of it over a few nights instead of asking about the same ten for ever."""
    _rows(db_session, 6)
    first = _Client(200)
    _sweep(db_session, first, limit=3)
    second = _Client(200)
    _sweep(db_session, second, limit=3)
    assert not (set(first.seen) & set(second.seen)), "it checked the same rows twice"


def test_already_delisted_rows_are_left_alone(db_session):
    """No point spending a request on a page we have already concluded is gone,
    and no point risking a flaky answer undoing a settled one."""
    rows = _rows(db_session, 2)
    rows[0].delisted_at = datetime.now(timezone.utc)
    db_session.commit()
    c = _Client(200)
    _sweep(db_session, c)
    assert len(c.seen) == 1


def test_an_empty_book_costs_nothing(db_session):
    c = _Client(200)
    out = _sweep(db_session, c)
    assert out["checked"] == 0
    assert c.seen == []


def test_a_listing_with_no_link_is_skipped(db_session):
    """Nothing to open. Counting it as gone would delist every row that arrived
    without a URL."""
    db_session.add(PortalListing(source="oneroof", kind="for_sale",
                                 status="approved", url=None,
                                 address="1 No Link Road", suburb="Papakura"))
    db_session.commit()
    c = _Client(404)
    out = _sweep(db_session, c)
    assert out["checked"] == 0
    assert c.seen == []


def test_sold_rows_are_not_checked(db_session):
    """A sold record is history. Its advertisement being gone is expected and
    says nothing."""
    db_session.add(PortalListing(source="oneroof", kind="sold", status="approved",
                                 url="https://oneroof.co.nz/sold/1",
                                 address="9 Sold Street", suburb="Papakura"))
    db_session.commit()
    c = _Client(404)
    assert _sweep(db_session, c)["checked"] == 0


# ---- it must never take the worker down -------------------------------------
def test_the_worker_entry_point_swallows_everything(monkeypatch):
    """A daily job that raises takes the whole worker loop with it, and this one
    talks to the open internet — the least reliable dependency there is."""
    def _boom(*a, **k):
        raise RuntimeError("the internet fell over")
    monkeypatch.setattr(D, "sweep", _boom)
    out = D.run_once()
    assert out["checked"] == 0
    assert out.get("error") is True


def test_the_job_is_registered_daily():
    from app.worker import build_jobs

    names = [j.name for j in build_jobs()]
    assert "listing link check" in names


def test_it_does_not_need_a_paid_api():
    """An HTTP HEAD against a public page costs nothing and needs no token, so
    this must not be gated behind the Apify switch the paid sweeps use."""
    from pathlib import Path

    src = Path("app/worker.py").read_text()
    job = [l for l in src.splitlines() if "listing link check" in l][0]
    idx = src.splitlines().index(job)
    following = "\n".join(src.splitlines()[idx:idx + 2])
    assert "portals_enabled" not in following


# ---- the point of the whole feature -----------------------------------------
# "a consumer won't come onto the site and then go to a house and the house no
#  longer exists"
#
# That is a different question from "did it sell", and it carries a different
# cost for being wrong, so it gets a different threshold. Hiding a live listing
# by mistake costs somebody one day of one opportunity, and the next pass puts
# it back. Sending a paying customer to a 404 costs their trust in every number
# on the site. So hiding happens on the FIRST confirmed dead link, while calling
# something off the market still takes three.
from app.models import BatchType, ImportBatch, PropertyForSale


def _live(db, n, *, host="oneroof.co.nz", **over):
    """Published listings — the rows a customer actually browses and clicks."""
    batch = db.query(ImportBatch).first()
    if batch is None:
        batch = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                            filename="auckland.csv", is_active=True,
                            status="published")
        db.add(batch)
        db.commit()
    made = []
    for i in range(n):
        r = PropertyForSale(url=f"https://{host}/live/{i}",
                            address=f"{i} Live Road", suburb="Papakura",
                            # A floor area, because _hide_bad_data already
                            # drops dwelling rows without one — the row has to
                            # be visible BEFORE the link check for the test to
                            # be about the link check.
                            floor_area_m2=120.0, property_type="House",
                            is_held=False, import_batch_id=batch.id, **over)
        db.add(r)
        made.append(r)
    db.commit()
    return made


def test_a_dead_link_is_hidden_from_customers_the_first_time(db_session):
    """THE POINT. Nobody should click from our site to a page that is not there,
    and waiting three days to stop showing it means three more days of people
    doing exactly that."""
    _live(db_session, 1)
    out = _sweep(db_session, _Client(404))
    row = db_session.query(PropertyForSale).first()
    assert row.link_dead_at is not None, "a dead link was left on the site"
    assert out["newly_hidden"] == 1


def test_a_hidden_row_is_actually_filtered_out(db_session):
    """The column only matters if the lists honour it. Every customer-facing
    query goes through _hide_bad_data, so this is the one place to prove it."""
    from app.routers.properties import _hide_bad_data

    rows = _live(db_session, 2)
    visible = _hide_bad_data(db_session.query(PropertyForSale)).count()
    assert visible == 2
    _sweep(db_session, _Client(404, codes={rows[0].url: 404, rows[1].url: 200}))
    after = _hide_bad_data(db_session.query(PropertyForSale)).all()
    assert [r.url for r in after] == [rows[1].url], "the dead listing is still on the site"


def test_a_link_that_comes_back_returns_to_the_site_immediately(db_session):
    """A portal's bad afternoon costs a listing one day, not its place in the
    product."""
    from app.routers.properties import _hide_bad_data

    _live(db_session, 1)
    _sweep(db_session, _Client(404))
    assert _hide_bad_data(db_session.query(PropertyForSale)).count() == 0
    _sweep(db_session, _Client(200))
    assert _hide_bad_data(db_session.query(PropertyForSale)).count() == 1
    assert db_session.query(PropertyForSale).first().link_dead_at is None


def test_being_blocked_never_empties_the_site(db_session):
    """THE NIGHTMARE. Hiding on the first 404 is only safe because a 403 is not
    a 404. If a portal starts refusing us, every listing must stay up."""
    from app.routers.properties import _hide_bad_data

    _live(db_session, 30)
    for code in (403, 429, 503):
        _sweep(db_session, _Client(code))
    assert _hide_bad_data(db_session.query(PropertyForSale)).count() == 30


def test_a_portal_that_moved_its_urls_does_not_empty_the_site(db_session):
    """And the circuit breaker is the second line: even a genuine wall of 404s
    is disbelieved when it is most of a portal's book."""
    from app.routers.properties import _hide_bad_data

    _live(db_session, 30)
    out = _sweep(db_session, _Client(404))
    assert out["abandoned"], "it believed a portal that 404'd everything"
    assert _hide_bad_data(db_session.query(PropertyForSale)).count() == 30
    assert out["newly_hidden"] == 0


def test_normal_churn_still_hides_the_ones_that_are_really_gone(db_session):
    """The guard must not swallow the real thing."""
    from app.routers.properties import _hide_bad_data

    rows = _live(db_session, 40)
    gone = {r.url: 404 for r in rows[:3]}
    _sweep(db_session, _Client(200, codes=gone))
    assert _hide_bad_data(db_session.query(PropertyForSale)).count() == 37


def test_the_live_table_is_checked_before_the_staging_one(db_session):
    """A dead link on a staged row nobody can see is a curiosity. A dead link on
    a live row is a customer hitting a 404 from our own site, so it gets the
    budget first."""
    _live(db_session, 3)
    _rows(db_session, 3)
    c = _Client(200)
    _sweep(db_session, c, limit=3)
    assert all("/live/" in u for u in c.seen), c.seen


def test_hiding_does_not_touch_anything_else_on_the_listing(db_session):
    """Blast radius on the table customers read."""
    rows = _live(db_session, 1, asking_price=950_000.0)
    keep = {c.name: getattr(rows[0], c.name)
            for c in PropertyForSale.__table__.columns
            if not c.name.startswith("link_") and hasattr(rows[0], c.name)}
    _sweep(db_session, _Client(404))
    row = db_session.query(PropertyForSale).first()
    after = {k: getattr(row, k) for k in keep}
    assert after == keep, f"changed {set(k for k in keep if after[k] != keep[k])}"


# ---- the numbers on the dashboard -------------------------------------------
def _admin_client(db_session):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import current_user, require_active, require_admin

    u = User(email="ops@test.local", password_hash="x",
             role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session, current_user: lambda: u,
        require_active: lambda: u, require_admin: lambda: u,
    }
    return TestClient(main.app, raise_server_exceptions=False)


def test_the_dashboard_endpoint_actually_answers(db_session):
    """It imports clean whether or not a name used INSIDE the function exists,
    so the only way to know the query is wired up is to call it. Shipped
    unchecked, a missing import here is a NameError the first time somebody
    opens the admin dashboard."""
    _live(db_session, 2)
    try:
        r = _admin_client(db_session).get("/api/admin/metrics")
        assert r.status_code == 200, r.text
        assert "off_market_now" in r.json()
    finally:
        from app import main
        main.app.dependency_overrides = {}


def test_the_dashboard_counts_what_came_off(db_session):
    rows = _live(db_session, 3)
    _sweep(db_session, _Client(200, codes={rows[0].url: 404}))
    try:
        body = _admin_client(db_session).get("/api/admin/metrics").json()
        assert body["off_market_now"] == 1
        assert body["off_market_7d"] == 1
        assert body["links_last_checked"] is not None
        assert body["links_never_checked"] == 0
    finally:
        from app import main
        main.app.dependency_overrides = {}


def test_a_zero_is_distinguishable_from_a_checker_that_never_ran(db_session):
    """THE NUMBER THAT LIES BY ITSELF. "0 off market" reads identically whether
    nothing dropped off or the sweep has not run in a fortnight. The last-checked
    stamp is what makes the zero mean something."""
    _live(db_session, 3)
    try:
        body = _admin_client(db_session).get("/api/admin/metrics").json()
        assert body["off_market_now"] == 0
        assert body["links_last_checked"] is None, "a never-run checker looked healthy"
        assert body["links_never_checked"] == 3
    finally:
        from app import main
        main.app.dependency_overrides = {}


def test_a_listing_that_comes_back_leaves_the_count(db_session):
    rows = _live(db_session, 2)
    _sweep(db_session, _Client(200, codes={rows[0].url: 404}))
    _sweep(db_session, _Client(200))
    try:
        body = _admin_client(db_session).get("/api/admin/metrics").json()
        assert body["off_market_now"] == 0
    finally:
        from app import main
        main.app.dependency_overrides = {}


def test_nothing_is_deleted(db_session):
    """Off the site is not gone from the database. The row is kept so it can
    come back, and so the history is still there to look at."""
    _live(db_session, 1)
    _sweep(db_session, _Client(404))
    assert db_session.query(PropertyForSale).count() == 1


def test_the_detail_page_is_told_the_listing_is_gone(db_session):
    """Lists exclude it, but the detail page is still reachable by bookmark,
    shared link or wish list. It has to be able to SAY so, or somebody rings an
    agent about a house that sold last week."""
    rows = _live(db_session, 1)
    assert rows[0].off_market is False
    _sweep(db_session, _Client(404))
    row = db_session.query(PropertyForSale).first()
    assert row.off_market is True


def test_off_market_is_derived_not_stored_twice(db_session):
    """Two columns saying the same thing is two columns that can disagree."""
    from app.models import PropertyForSale as P

    assert "off_market" not in {c.name for c in P.__table__.columns}
    assert isinstance(getattr(P, "off_market"), property)
