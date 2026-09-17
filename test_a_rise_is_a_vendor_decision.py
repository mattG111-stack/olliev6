"""A price rise has to be something a vendor did.

    "3 Rangeview Road  $1.27M -> $1.41M  +11.0%
     30b Tawa Crescent  $830k -> $900k  +8.4%
     33C Garadice Road  $1.10M -> $1.18M  +7.3%
     77A Buckland Road  $880k -> $920k  +4.5%
     77B Buckland Road  $870k -> $900k  +3.4%
     these are wrong"

Five vendors raising their price in one week is not a market, it is a bug. The
last time this panel was wrong the numbers were +501%, +350%, +294% — obviously
broken, and fixed by refusing prices the SCRAPER invented: the council valuation
copied into the price field, or the last sale price.

This is the same fault one step further in, and it produces believable wrong
answers instead of absurd ones. The third way a number lands in asking_price
without a vendor naming it is that WE put it there: when a vendor withdraws
their price, the carry-forward writes their last advertised figure less 3%,
rounded to the nearest $10,000, so the listing keeps a working price. Compare
that derived number against a real one the following week and the difference is
printed as a decision nobody made — a few percent, in round thousands, exactly
like the list above.

Two panels compute this, and only one of them had ever been fixed. The
/batches/compare endpoint had no placeholder filter, no derived filter, no
believability band, and its "risers" query sorted descending WITHOUT requiring
the change to be a rise — so in a week where every price fell, the least-bad
drop was printed as the biggest rise. One rule, two callers, one of them wrong;
they share the rule now.
"""
from __future__ import annotations

import pytest

from app.models import BatchType, ImportBatch, PropertyForSale
from app.prior_price import ADVERTISED_BASIS, DERIVED_BASIS


def _batch(db, *, active=False):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="week.csv", status="published", is_active=active)
    db.add(b)
    db.flush()
    return b


def _row(db, batch, slug, price, *, basis=ADVERTISED_BASIS, cv=None,
         last_sold=None, address="1 Test Road"):
    db.add(PropertyForSale(
        import_batch_id=batch.id, slug_id=slug, address=address, suburb="Sunnyvale",
        property_type="House", asking_price=price, asking_basis=basis,
        cv_numeric=cv, valuation_last_sold_value=last_sold,
        floor_area_m2=140.0, is_held=False))


@pytest.fixture()
def two_weeks(db_session):
    """Last week and this week, with one honest rise and four impostors."""
    prev, curr = _batch(db_session), _batch(db_session, active=True)

    # THE ONE REAL RISE. A vendor advertised a price and then advertised a
    # higher one. This is the only row that belongs in the panel.
    _row(db_session, prev, "real", 1_000_000.0, address="1 Honest Way")
    _row(db_session, curr, "real", 1_050_000.0, address="1 Honest Way")

    # THE BUG. We derived last week's price because the vendor had withdrawn
    # theirs; this week they advertised again. Nobody raised anything.
    _row(db_session, prev, "derived", 870_000.0, basis=DERIVED_BASIS,
         address="77B Buckland Road")
    _row(db_session, curr, "derived", 900_000.0, address="77B Buckland Road")

    # ...and the mirror: real last week, derived this week.
    _row(db_session, prev, "derived2", 830_000.0, address="30b Tawa Crescent")
    _row(db_session, curr, "derived2", 900_000.0, basis=DERIVED_BASIS,
         address="30b Tawa Crescent")

    # The already-known impostors, asserted so the earlier fix cannot be undone
    # by this one: the council valuation copied into the price field...
    _row(db_session, prev, "cv", 1_200_000.0, cv=1_200_000.0,
         address="3 Rangeview Road")
    _row(db_session, curr, "cv", 1_410_000.0, address="3 Rangeview Road")
    # ...and the last sale price copied into it.
    _row(db_session, prev, "sale", 1_100_000.0, last_sold=1_100_000.0,
         address="33C Garadice Road")
    _row(db_session, curr, "sale", 1_180_000.0, address="33C Garadice Road")

    db_session.commit()
    return prev, curr


def _rises(client, prev, curr):
    """The rises panel on the daily brief — the one with five entries on it.

    Asserted through /today rather than /batches/compare because that endpoint
    is written in Postgres-only SQL (FULL OUTER JOIN, PERCENTILE_CONT, FILTER)
    and cannot run here at all. The same fix is applied to both; only this one
    can be proved. That is worth knowing rather than hiding: the compare
    endpoint has never been exercised by a test, which is exactly how it kept
    four faults the brief had already had fixed.
    """
    r = client.get("/api/dashboards/today")
    assert r.status_code == 200, r.text
    return r.json()["biggest_rises"]


def test_a_price_we_derived_is_not_a_price_the_vendor_raised(client, two_weeks):
    """THE BUG, named. 77B Buckland Road at +3.4% — a number we wrote down
    ourselves last week, subtracted from a real one this week."""
    prev, curr = two_weeks
    shown = {row["slug_id"] for row in _rises(client, prev, curr)}

    assert "derived" not in shown, \
        "a carried-forward price was printed as a vendor putting their price up"
    assert "derived2" not in shown, \
        "a price we derived THIS week was printed as a rise"


def test_the_scraper_impostors_are_still_refused(client, two_weeks):
    """The earlier fix, asserted here so this change cannot quietly undo it."""
    prev, curr = two_weeks
    shown = {row["slug_id"] for row in _rises(client, prev, curr)}

    assert "cv" not in shown, "the council valuation is back in the price field"
    assert "sale" not in shown, "the last sale price is back in the price field"


def test_a_real_rise_still_shows(client, two_weeks):
    """The counterweight. A panel that refuses everything is not a fixed panel,
    and every one of these guards is a way to end up with an empty page."""
    prev, curr = two_weeks
    shown = {row["slug_id"] for row in _rises(client, prev, curr)}

    assert shown == {"real"}, f"expected only the genuine rise, got {shown}"


def test_a_falling_week_reports_no_rises_rather_than_the_least_bad_drop(
        client, db_session):
    """Sorting descending only puts the largest number first. In a week where
    every price fell, the largest number is the smallest DROP — and it was
    printed under "biggest price rises"."""
    prev, curr = _batch(db_session), _batch(db_session, active=True)
    _row(db_session, prev, "a", 1_000_000.0)
    _row(db_session, curr, "a", 950_000.0)          # −5%
    _row(db_session, prev, "b", 1_000_000.0)
    _row(db_session, curr, "b", 800_000.0)          # −20%
    db_session.commit()

    assert _rises(client, prev, curr) == [], \
        "a week of falling prices produced a list of rises"


def test_an_unbelievable_move_is_left_out(client, db_session):
    """A price that doubles between two weekly loads is not a re-price, and
    whatever it is, it is not something to headline as one."""
    prev, curr = _batch(db_session), _batch(db_session, active=True)
    _row(db_session, prev, "wild", 500_000.0)
    _row(db_session, curr, "wild", 3_000_000.0)     # +500%
    db_session.commit()

    assert _rises(client, prev, curr) == []


def test_a_listing_with_no_council_record_is_not_thrown_away(client, db_session):
    """Three-valued logic: NOT (NULL > 0 AND ...) is NULL, and the WHERE drops
    the row. Without COALESCE the placeholder filter also silently discards
    every listing that has no CV — which is a lot of them."""
    prev, curr = _batch(db_session), _batch(db_session, active=True)
    _row(db_session, prev, "nocv", 900_000.0, cv=None, last_sold=None)
    _row(db_session, curr, "nocv", 950_000.0, cv=None, last_sold=None)
    db_session.commit()

    assert {r["slug_id"] for r in _rises(client, prev, curr)} == {"nocv"}


def test_both_panels_use_the_same_rule():
    """The fault was two spellings of one idea, and only one of them fixed.
    Asserted structurally so a third caller cannot be written by hand."""
    import inspect

    from app.routers import dashboards as D

    src = inspect.getsource(D)
    assert src.count("_REAL_ASKING_SQL = ") == 1, "the rule has been written twice again"
    # Both the daily brief's movers and the batch-compare endpoint.
    assert src.count("+ _REAL_ASKING_SQL +") >= 4, \
        "a price comparison is not going through the shared rule"


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import current_user, require_active

    user = User(email="movers@test.local", password_hash="x",
                role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(user)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: user,
        require_active: lambda: user,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}
