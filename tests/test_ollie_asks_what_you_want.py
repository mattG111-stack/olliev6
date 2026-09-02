"""What a customer is hunting: asked once, re-checked every fortnight.

    "do we up the user experience and ask what they are looking for at the
     start which also helps us understand our customer ... and then all that is
     saved into the backend so we know our customer"
    "under there user and its when they first login and at least every two
     weeks you ask them if anything has changed with their recommences"
    "i think those questions are only when they go into ollie"

So: not a signup wizard, not a saved search the customer has to remember to
create — a short conversation the first time they open Ollie, stored against
their user record, and re-confirmed every fourteen days.

The things that can go wrong here are all quiet ones, which is why they are
each pinned down below:

  * A clock that never restarts asks the same question every single visit.
  * A clock that restarts on the wrong event never asks again.
  * Naive vs aware datetimes: Postgres returns one, SQLite the other, and
    comparing them raises — the exact failure that shipped in the enrich
    liveness check.
  * Goals AND-ed instead of OR-ed produce an empty personalised feed, which
    reads to a customer as a broken product rather than a narrow filter.
  * A count quoted to a customer that does not match what they can then open.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import preferences as prefs
from app.models import (BatchType, ImportBatch, PropertyForSale, User,
                        UserRole, UserStatus)

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


# ---- the clock --------------------------------------------------------------
def test_a_new_customer_is_asked():
    u = User(email="new@test.local", password_hash="x")
    assert prefs.state(u, NOW) == "unset"


def test_answering_stops_the_asking():
    u = User(email="a@test.local", password_hash="x")
    prefs.apply(u, goals=["underpriced"], suburbs=["Remuera"], now=NOW)
    assert prefs.state(u, NOW) == "current"


def test_thirteen_days_later_they_are_left_alone():
    u = User(email="b@test.local", password_hash="x")
    prefs.apply(u, goals=["underpriced"], now=NOW)
    assert prefs.state(u, NOW + timedelta(days=13)) == "current"


def test_a_fortnight_later_we_ask_again():
    """"at least every two weeks" — the ceiling on how stale a stated
    preference is allowed to get."""
    u = User(email="c@test.local", password_hash="x")
    prefs.apply(u, goals=["underpriced"], now=NOW)
    assert prefs.state(u, NOW + timedelta(days=14, minutes=1)) == "due"


def test_confirming_restarts_the_fortnight():
    """"Nothing's changed" is an answer. Without this the check-in reappears on
    every single visit, which is how a helpful question becomes a nag."""
    u = User(email="d@test.local", password_hash="x")
    prefs.apply(u, goals=["underpriced"], now=NOW)
    later = NOW + timedelta(days=15)
    assert prefs.state(u, later) == "due"
    prefs.confirm(u, now=later)
    assert prefs.state(u, later) == "current"
    assert prefs.state(u, later + timedelta(days=15)) == "due"


def test_confirming_does_not_erase_what_they_told_us():
    u = User(email="e@test.local", password_hash="x")
    prefs.apply(u, goals=["subdividable"], suburbs=["Papakura"],
                min_price=800_000, max_price=1_400_000, now=NOW)
    prefs.confirm(u, now=NOW + timedelta(days=15))
    s = prefs.summary(u)
    assert s["goals"] == ["subdividable"]
    assert s["suburbs"] == ["Papakura"]
    assert s["max_price"] == 1_400_000


def test_snoozing_pushes_past_the_fortnight():
    """"Ask me again in a month" has to actually mean a month, or the option is
    a lie the second time they see the question."""
    u = User(email="f@test.local", password_hash="x")
    prefs.apply(u, goals=["underpriced"], now=NOW)
    prefs.snooze(u, now=NOW)
    assert prefs.state(u, NOW + timedelta(days=20)) == "current"
    assert prefs.state(u, NOW + timedelta(days=31)) == "due"


def test_changing_your_mind_clears_a_snooze():
    """Someone who snoozed and then edited anyway is engaged, not avoiding us —
    the fortnight should run from the edit, not stay parked a month out."""
    u = User(email="g@test.local", password_hash="x")
    prefs.apply(u, goals=["underpriced"], now=NOW)
    prefs.snooze(u, now=NOW)
    prefs.apply(u, goals=["cashflow"], now=NOW + timedelta(days=1))
    assert u.preferences_snoozed_until is None
    assert prefs.state(u, NOW + timedelta(days=20)) == "due"


def test_the_first_answer_is_remembered_separately_from_the_last():
    """set_at never moves. It is the only way the admin page can tell a fresh
    answer from one that has been through a check-in."""
    u = User(email="h@test.local", password_hash="x")
    prefs.apply(u, goals=["underpriced"], now=NOW)
    first = u.preferences_set_at
    prefs.apply(u, goals=["cashflow"], now=NOW + timedelta(days=30))
    assert u.preferences_set_at == first
    assert u.preferences_reviewed_at == NOW + timedelta(days=30)


def test_a_naive_timestamp_does_not_raise():
    """SQLite hands back naive datetimes and Postgres aware ones. Comparing the
    two raises TypeError — which would 500 the very first call the Ask page
    makes, on the test database only."""
    u = User(email="i@test.local", password_hash="x")
    u.preferences_reviewed_at = datetime(2026, 8, 1, 9, 0)   # no tzinfo
    assert prefs.state(u, NOW) == "due"


# ---- what may be stored -----------------------------------------------------
def test_a_goal_we_have_never_heard_of_is_dropped():
    assert prefs.clean_goals(["underpriced", "beachfront-castle"]) == ["underpriced"]


def test_goals_come_back_in_a_settled_order():
    """So two customers who picked the same two things are one group on the
    admin page, not two."""
    assert (prefs.clean_goals(["cashflow", "underpriced"])
            == prefs.clean_goals(["underpriced", "cashflow"]))


def test_a_suburb_with_a_comma_survives_the_round_trip():
    """The feeds spell some areas "Remuera, Auckland". Comma-splitting would
    turn one suburb into two, both of which match nothing."""
    stored = prefs.dump_list(["Remuera, Auckland", "Papakura"])
    assert prefs.parse_list(stored) == ["Remuera, Auckland", "Papakura"]


def test_the_same_suburb_twice_is_stored_once():
    assert prefs.parse_list(prefs.dump_list(["Remuera", "remuera "])) == ["Remuera"]


def test_a_backwards_budget_is_read_the_way_it_was_meant():
    """Typing the big number first is a slip, not a request for nothing."""
    u = User(email="j@test.local", password_hash="x")
    prefs.apply(u, min_price=1_400_000, max_price=800_000, now=NOW)
    s = prefs.summary(u)
    assert (s["min_price"], s["max_price"]) == (800_000, 1_400_000)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5, 0, "banana", None])
def test_a_price_that_is_not_a_price_is_stored_as_nothing(bad):
    """NaN is truthy and int(NaN) raises — a null arriving from a float column
    has broken this codebase before."""
    assert prefs.clean_price(bad) is None


def test_a_budget_bigger_than_the_country_is_capped():
    assert prefs.clean_price(1e30) == prefs.MAX_PRICE


def test_a_thousand_suburbs_do_not_all_get_stored():
    """The column is text, not a bucket. A scripted client should not be able
    to make one user row megabytes wide."""
    stored = prefs.parse_list(prefs.dump_list([f"Suburb {i}" for i in range(1000)]))
    assert len(stored) == prefs.MAX_AREAS


def test_nothing_stored_reads_back_as_nothing():
    u = User(email="k@test.local", password_hash="x")
    s = prefs.summary(u)
    assert s == {"goals": [], "suburbs": [], "districts": [],
                 "min_price": None, "max_price": None, "min_beds": None}


# ---- through the API --------------------------------------------------------
@pytest.fixture()
def world(db_session):
    """A live batch: two underpriced houses in Remuera, one splittable site in
    Papakura, one dear house nobody's budget reaches, and one held row."""
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="week.csv", status="published", is_active=True)
    db_session.add(b)
    db_session.flush()
    rows = [
        PropertyForSale(
            import_batch_id=b.id, slug_id="u1", address="1 Cheap Street",
            suburb="Remuera", district="Auckland City", property_type="House",
            beds=3, asking_price=900_000.0, cv_numeric=1_000_000.0,
            fair_value=1_100_000.0, margin=0.22, floor_area_m2=150.0,
            is_underpriced=True, is_subdividable=False, is_held=False),
        PropertyForSale(
            import_batch_id=b.id, slug_id="u2", address="2 Cheap Street",
            suburb="Remuera", district="Auckland City", property_type="House",
            beds=4, asking_price=1_000_000.0, cv_numeric=1_100_000.0,
            fair_value=1_150_000.0, margin=0.15, floor_area_m2=160.0,
            is_underpriced=True, is_subdividable=False, is_held=False),
        PropertyForSale(
            import_batch_id=b.id, slug_id="s1", address="3 Wide Section Road",
            suburb="Papakura", district="Papakura", property_type="House",
            beds=3, asking_price=1_200_000.0, cv_numeric=1_250_000.0,
            fair_value=1_260_000.0, margin=0.05, floor_area_m2=140.0,
            is_underpriced=False, is_subdividable=True, max_addl_lots=2.0,
            is_held=False),
        PropertyForSale(
            import_batch_id=b.id, slug_id="x1", address="4 Far Too Dear Drive",
            suburb="Remuera", district="Auckland City", property_type="House",
            beds=5, asking_price=4_000_000.0, cv_numeric=4_200_000.0,
            fair_value=4_400_000.0, margin=0.10, floor_area_m2=320.0,
            is_underpriced=True, is_subdividable=True, is_held=False),
        PropertyForSale(
            import_batch_id=b.id, slug_id="h1", address="5 Held Back Lane",
            suburb="Remuera", district="Auckland City", property_type="House",
            beds=3, asking_price=800_000.0, cv_numeric=900_000.0,
            fair_value=1_600_000.0, margin=1.0, floor_area_m2=150.0,
            is_underpriced=True, is_subdividable=True, is_held=True,
            hold_reason="Hidden by admin"),
    ]
    db_session.add_all(rows)
    db_session.commit()
    return b


def _client(db_session, user):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.security import current_user, require_active, require_admin

    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: user,
        require_active: lambda: user,
        require_admin: lambda: user,
    }
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture()
def customer(db_session):
    from app import main

    u = User(email="hunter@test.local", password_hash="x",
             role=UserRole.USER.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    try:
        yield _client(db_session, u), u
    finally:
        main.app.dependency_overrides = {}


def test_a_new_customer_is_told_we_have_not_asked_yet(customer):
    c, _ = customer
    r = c.get("/api/preferences")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "unset"


def test_what_they_answer_comes_back_on_the_next_visit(customer):
    """The whole point of storing it against the user: they say it once."""
    c, _ = customer
    saved = c.put("/api/preferences", json={
        "goals": ["underpriced", "subdividable"],
        "suburbs": ["Remuera", "Papakura"],
        "districts": [],
        "min_price": 850_000, "max_price": 1_400_000, "min_beds": 3,
    })
    assert saved.status_code == 200, saved.text
    again = c.get("/api/preferences").json()
    assert again["goals"] == ["underpriced", "subdividable"]
    assert again["suburbs"] == ["Remuera", "Papakura"]
    assert again["max_price"] == 1_400_000
    assert again["state"] == "current"


def test_saving_counts_as_answering(customer):
    """Someone who has just told us what they want must not be asked again a
    moment later."""
    c, _ = customer
    c.put("/api/preferences", json={"goals": ["underpriced"]})
    assert c.get("/api/preferences").json()["state"] == "current"


def test_skipping_is_an_answer_too(customer):
    """"Just show me everything" stops the question coming back tomorrow, and
    stores no criteria so nothing is filtered."""
    c, _ = customer
    r = c.post("/api/preferences/skip")
    assert r.status_code == 200, r.text
    body = c.get("/api/preferences").json()
    assert body["state"] == "current"
    assert body["goals"] == [] and body["suburbs"] == []


def test_the_check_in_comes_back_round(customer, db_session):
    c, u = customer
    c.put("/api/preferences", json={"goals": ["underpriced"]})
    u.preferences_reviewed_at = datetime.now(timezone.utc) - timedelta(days=15)
    db_session.commit()
    assert c.get("/api/preferences").json()["state"] == "due"
    assert c.post("/api/preferences/confirm").json()["state"] == "current"


def test_the_options_carry_what_each_area_actually_holds(customer, world):
    """A chooser with counts turns a guess into a decision — and an area with
    nothing in it can never be picked in silence."""
    c, _ = customer
    r = c.get("/api/preferences/options")
    assert r.status_code == 200, r.text
    counts = {s["suburb"]: s["count"] for s in r.json()["suburbs"]}
    # Four visible rows: two Remuera, one Papakura, one dear Remuera. The held
    # one is not offered, because it is not there to be found.
    assert counts == {"Remuera": 3, "Papakura": 1}


def test_the_budget_slider_is_offered_the_real_distribution(customer, world):
    c, _ = customer
    body = c.get("/api/preferences/options").json()
    assert sum(body["price_buckets"]) > 0
    assert len(body["price_bucket_edges"]) == len(body["price_buckets"]) + 1


def test_two_goals_mean_both_kinds_not_only_the_overlap(customer, world):
    """THE BUG THIS PREVENTS. Someone who wants underpriced houses AND land
    they can split wants both lists. AND-ing them returns only the rare
    property that is both — a personalised feed that comes back nearly empty
    reads as broken, not as precise."""
    c, _ = customer
    r = c.post("/api/preferences/preview", json={
        "goals": ["underpriced", "subdividable"],
        "suburbs": ["Remuera", "Papakura"],
        "min_price": 800_000, "max_price": 1_400_000,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # 1 Cheap St + 2 Cheap St (underpriced) + 3 Wide Section Rd (splittable).
    assert body["matches"] == 3, body


def test_the_preview_counts_only_what_they_could_open(customer, world):
    """The held row has the biggest margin in the batch by far. If the number we
    quote counts it, the customer opens the list and it is not there."""
    c, _ = customer
    body = c.post("/api/preferences/preview", json={
        "goals": ["underpriced"], "suburbs": ["Remuera"],
    }).json()
    addresses = {row["address"] for row in body["rows"]}
    assert "5 Held Back Lane" not in addresses
    # Three underpriced Remuera rows are visible; the held one would make four.
    assert body["matches"] == 3


def test_a_budget_actually_narrows_it(customer, world):
    c, _ = customer
    wide = c.post("/api/preferences/preview", json={
        "goals": ["underpriced"], "suburbs": ["Remuera"]}).json()
    tight = c.post("/api/preferences/preview", json={
        "goals": ["underpriced"], "suburbs": ["Remuera"],
        "max_price": 950_000}).json()
    assert wide["matches"] == 3 and tight["matches"] == 1


def test_the_in_budget_total_ignores_the_goals(customer, world):
    """The "571 of the listings I watch sit in that range" line is about the
    area and the money, before the goals narrow it. Reporting the narrowed
    number there would tell them their budget was tighter than it is."""
    c, _ = customer
    body = c.post("/api/preferences/preview", json={
        "goals": ["cashflow"], "suburbs": ["Remuera"],
        "min_price": 100_000, "max_price": 2_000_000}).json()
    assert body["matches"] == 0        # nothing here is cashflow positive
    assert body["in_budget"] == 2      # but two are in their range


def test_the_answer_leads_with_the_biggest_gap(customer, world):
    c, _ = customer
    body = c.post("/api/preferences/preview", json={
        "goals": ["underpriced"], "suburbs": ["Remuera"],
        "max_price": 1_400_000}).json()
    assert body["rows"][0]["address"] == "1 Cheap Street"
    assert body["best_margin_dollars"] == pytest.approx(200_000)


def test_a_hostile_budget_does_not_break_the_preview(customer, world):
    """Straight from the address bar into a WHERE clause otherwise."""
    c, _ = customer
    r = c.post("/api/preferences/preview",
               json={"min_price": 1e308, "max_price": -4, "min_beds": 99999})
    assert r.status_code == 200, r.text


def test_asking_for_nothing_in_particular_is_the_whole_market(customer, world):
    c, _ = customer
    body = c.post("/api/preferences/preview", json={}).json()
    assert body["matches"] == 4      # everything visible, held row excluded


def test_a_signed_out_visitor_cannot_read_a_profile(db_session):
    """These are the customer's own answers about their money."""
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db

    main.app.dependency_overrides = {get_db: lambda: db_session}
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        assert c.get("/api/preferences").status_code in (401, 403)
    finally:
        main.app.dependency_overrides = {}


# ---- what it tells us -------------------------------------------------------
@pytest.fixture()
def admin(db_session):
    from app import main

    u = User(email="boss@test.local", password_hash="x",
             role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    try:
        yield _client(db_session, u)
    finally:
        main.app.dependency_overrides = {}


def _hunters(db_session, n, **kw):
    made = []
    for i in range(n):
        u = User(email=f"h{len(made)}-{kw.get('tag','x')}-{i}@test.local",
                 password_hash="x", role=UserRole.USER.value,
                 status=UserStatus.APPROVED.value)
        prefs.apply(u, goals=kw.get("goals"), suburbs=kw.get("suburbs"),
                    max_price=kw.get("max_price"), now=NOW)
        db_session.add(u)
        made.append(u)
    db_session.commit()
    return made


def test_the_aggregate_counts_what_people_asked_for(admin, db_session, world):
    _hunters(db_session, 4, tag="a", goals=["underpriced"], suburbs=["Remuera"],
             max_price=1_200_000)
    _hunters(db_session, 2, tag="b", goals=["subdividable"], suburbs=["Pukekohe"],
             max_price=1_600_000)
    body = admin.get("/api/admin/customer-intel").json()
    counts = {g["key"]: g["count"] for g in body["goals"]}
    assert counts["underpriced"] == 4
    assert counts["subdividable"] == 2


def test_the_aggregate_never_names_anybody(admin, db_session, world):
    """An admin page that lists who wants what invites being used as a prospect
    list. This one can only be used to decide what to build."""
    _hunters(db_session, 3, tag="c", goals=["underpriced"], suburbs=["Remuera"])
    raw = admin.get("/api/admin/customer-intel").text
    assert "@test.local" not in raw
    assert "email" not in raw.lower()


def test_demand_is_reported_against_what_we_hold(admin, db_session, world):
    """The point of the page: four people watching a suburb we hold nothing in
    is the next region to ingest."""
    _hunters(db_session, 4, tag="d", goals=["subdividable"], suburbs=["Pukekohe"])
    body = admin.get("/api/admin/customer-intel").json()
    rows = {a["suburb"]: a for a in body["areas"]}
    assert rows["Pukekohe"]["watchers"] == 4
    assert rows["Pukekohe"]["listings"] == 0
    assert rows["Pukekohe"]["verdict"] == "thin"
    assert "Pukekohe" in body["gap_suburbs"]


def test_a_suburb_we_cover_is_not_reported_as_a_gap(admin, db_session, world):
    """The counterweight — a page that calls everything a gap says nothing."""
    _hunters(db_session, 1, tag="e", goals=["underpriced"], suburbs=["Remuera"])
    body = admin.get("/api/admin/customer-intel").json()
    assert "Remuera" not in body["gap_suburbs"]


def test_people_who_were_never_asked_do_not_count_as_refusals(admin, db_session):
    """The response rate has to be honest: someone who has not opened Ollie yet
    has not declined to answer."""
    quiet = User(email="quiet@test.local", password_hash="x",
                 role=UserRole.USER.value, status=UserStatus.APPROVED.value)
    db_session.add(quiet)
    _hunters(db_session, 1, tag="f", goals=["underpriced"])
    db_session.commit()
    body = admin.get("/api/admin/customer-intel").json()
    assert body["customers"] == 2
    assert body["answered"] == 1


def test_a_customer_cannot_read_the_aggregate(customer, db_session):
    """It is a business view, not a product feature."""
    c, _ = customer
    from app import main
    from app.db import get_db
    from app.security import current_user, require_active
    # Drop the admin override this module's helper installs, so the real
    # require_admin runs against a plain customer.
    u = db_session.query(User).filter(User.email == "hunter@test.local").first()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: u,
        require_active: lambda: u,
    }
    assert c.get("/api/admin/customer-intel").status_code in (401, 403)
