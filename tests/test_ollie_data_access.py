"""Ollie should be able to reach the data that describes property.

    "ollie should have access to all the data in the system and be able to
     answer any question"

It could reach four tables out of twenty-two, and only three of those four were
described in the schema it is handed. The fourth — properties_rent — was on the
allowlist and documented nowhere, which is the same as not being reachable at
all: a model cannot query a table it has never been told exists.

So the rental data was loaded every week, priced against in the cashflow
figures, and unanswerable. "What does a 3-bed in Glenfield rent for" got
answered out of properties_for_sale.est_weekly_rent — our own estimate, for a
house that is for sale, which is not a rental and not an observation.

The line drawn here is not "property data vs the rest". It is houses and the
market on one side, and people and secrets on the other. Password hashes, saved
API keys, customer names and emails in the referral tables, one person's saved
searches, another person's questions — a read-only transaction is no protection
against any of those, because SELECT is exactly the dangerous verb. The
allowlist is the control, so these tests assert both halves of it.
"""
from __future__ import annotations

import pytest

from app.assistant import sql as sqltool
from app.assistant.tools import TOOL_SPECS, _HANDLERS, rent_estimate
from app.models import BatchType, ImportBatch, PropertyRent

GAP = "CANNOT ANSWER YET"


def _rentals(db, n=8, **over):
    b = ImportBatch(batch_type=BatchType.RENT.value, region="Auckland",
                    filename="rent.xlsx", is_active=True, status="published")
    db.add(b)
    db.flush()
    spec = dict(suburb="Glenfield", district="North Shore City",
                property_type="House", beds=3, rent=780)
    spec.update(over)
    for i in range(n):
        db.add(PropertyRent(
            import_batch_id=b.id, address=f"{i} Chartwell Ave",
            suburb=spec["suburb"], district=spec["district"],
            property_type=spec["property_type"], beds=spec["beds"], baths=1,
            weekly_rent=spec["rent"] + i * 10))
    db.commit()
    return b


# ---- the tables that were unreachable --------------------------------------
def test_every_allowed_table_is_described_in_the_schema():
    """An allowlisted table the model is never told about is unreachable.

    That is exactly what happened to properties_rent: on the list, in no
    sentence of the schema, and therefore never queried once.
    """
    for table in sqltool.ALLOWED_TABLES:
        assert table in sqltool.SCHEMA, f"{table} is queryable but undocumented"


def test_the_property_tables_are_all_reachable():
    for table in ("properties_for_sale", "properties_sold", "properties_rent",
                  "portal_listings", "portal_findings", "parcel_cache",
                  "building_overrides"):
        assert table in sqltool.ALLOWED_TABLES


def test_data_freshness_is_reachable():
    """"Is this data current" is a fair question and needs the load history."""
    assert "import_batches" in sqltool.ALLOWED_TABLES
    assert "ingest_jobs" in sqltool.ALLOWED_TABLES


# ---- and the ones that must stay out ---------------------------------------
@pytest.mark.parametrize("table", [
    "users",                # password hashes
    "verification_codes",   # live login codes
    "app_settings",         # encrypted API tokens, including Apify's
    "promoters", "referrals", "referral_clicks", "commissions",  # names, emails, money
    "assistant_logs",       # what other people asked, in their own words
    "wish_lists",           # one person's saved searches, and no user scoping here
    "bug_reports",          # free text, so whatever a reporter pasted in
])
def test_people_and_secrets_stay_out(table):
    assert table not in sqltool.ALLOWED_TABLES
    with pytest.raises(sqltool.UnsafeQuery):
        sqltool.validate(f"SELECT * FROM {table}")


def test_a_blocked_table_cannot_be_reached_through_a_join():
    with pytest.raises(sqltool.UnsafeQuery):
        sqltool.validate("SELECT p.address, u.password_hash FROM properties_sold p "
                         "JOIN users u ON u.id = p.id")


def test_a_blocked_table_cannot_be_reached_through_a_subquery():
    with pytest.raises(sqltool.UnsafeQuery):
        sqltool.validate("SELECT address FROM properties_sold WHERE id IN "
                         "(SELECT id FROM users)")


def test_a_blocked_table_cannot_be_reached_through_a_union():
    with pytest.raises(sqltool.UnsafeQuery):
        sqltool.validate("SELECT address FROM properties_sold "
                         "UNION SELECT email FROM users")


def test_the_schema_tells_the_model_what_it_cannot_read():
    """So it says so plainly instead of retrying the same question five ways."""
    assert "NOT QUERYABLE" in sqltool.SCHEMA


def test_the_new_tables_are_actually_queryable():
    for t in ("portal_listings", "parcel_cache", "ingest_jobs", "properties_rent"):
        assert sqltool.validate(f"SELECT * FROM {t}").lower().startswith("select")


# ---- rent, which is the question it could not answer at all ----------------
def test_it_answers_a_rent_question_from_real_rentals(db_session):
    _rentals(db_session)
    out = rent_estimate(suburb="Glenfield", beds=3)
    assert "$" in out
    assert "a week" in out
    assert "ADVERTISED" in out


def test_it_says_the_rent_is_observed_not_our_estimate(db_session):
    """The wrong answer this replaces came from est_weekly_rent — our own
    number for a house that is for sale. Saying which is which is the point."""
    _rentals(db_session)
    out = rent_estimate(suburb="Glenfield", beds=3)
    assert "not our estimate" in out.lower()


def test_it_says_how_it_matched(db_session):
    _rentals(db_session)
    out = rent_estimate(suburb="Glenfield", beds=3)
    assert "same suburb" in out
    assert "_" not in out.split("—")[1].split(".")[0]      # no tier variable names


def test_a_suburb_figure_is_never_quoted_as_an_answer_about_a_size(db_session):
    """The cascade's last suburb tier ignores bed count on purpose — as a
    cashflow input a suburb median beats nothing. Quoted back to someone who
    asked about a nine-bedroom, it answered "$815 a week" off a suburb of
    three-bedrooms. That is not a ballpark, it is the wrong number hedged."""
    _rentals(db_session, beds=3)
    out = rent_estimate(suburb="Glenfield", beds=9)
    assert GAP in out
    assert "$815" not in out


def test_a_suburb_wide_question_still_gets_a_suburb_wide_answer(db_session):
    """The same tier is the right answer when no size was asked about."""
    _rentals(db_session, beds=3)
    out = rent_estimate(suburb="Glenfield")
    assert GAP not in out
    assert "$" in out


def test_too_few_of_that_size_asks_to_widen_rather_than_quoting_two(db_session):
    _rentals(db_session, n=2, beds=5)
    out = rent_estimate(suburb="Glenfield", beds=5)
    assert GAP in out
    assert "widen" in out.lower()


def test_an_unknown_suburb_offers_the_real_spellings(db_session):
    _rentals(db_session, suburb="Glenfield")
    out = rent_estimate(suburb="Glenfeild", beds=3)
    assert GAP in out
    assert "Glenfield" in out


def test_no_rent_data_loaded_says_what_to_publish(db_session):
    out = rent_estimate(suburb="Glenfield", beds=3)
    assert GAP in out
    assert "publish" in out.lower()


# ---- the tool is declared, so the model can actually reach it ---------------
def test_the_rent_tool_is_declared_and_dispatchable():
    names = [t["name"] for t in TOOL_SPECS]
    assert "rent_estimate" in names
    assert "rent_estimate" in _HANDLERS


def test_the_rent_tool_warns_against_the_wrong_column():
    """est_weekly_rent is right there on every listing and looks like an
    answer. The spec has to say out loud that it isn't one."""
    spec = next(t for t in TOOL_SPECS if t["name"] == "rent_estimate")
    assert "est_weekly_rent" in spec["description"]


def test_every_declared_tool_has_a_handler():
    for spec in TOOL_SPECS:
        assert spec["name"] in _HANDLERS, spec["name"]
