"""The headline card and the list under it have to be the same ranking.

    "and subdivied and under priced should be biggest margin first"
    "it should be biggest net gain all ways"

Two faults, and they compounded.

  THE HERO WAS PICKED ON A DIFFERENT AXIS. The row list sorts by whatever the
  page asks for. The summary that chooses the headline card had its own,
  separate rule — a two-case guess: max_addl_lots, or else margin. The
  subdividable page sorts by best_net_gain, which is neither, so it fell through
  to margin. The card at the top of that page was the biggest UNDERPRICING in
  the list while every row beneath it was ordered by subdivision profit. The top
  of the page disagreed with the page.

  THE UNDERPRICED PAGE RANKED ON A PERCENTAGE. A 24% margin on a $600k unit is
  $144k; a 12% margin on a $3M home is $360k. Sorted on the percentage the
  smaller one comes first, which is not what anyone is looking for when they
  open a list of deals. Both pages now rank on the money.

One sort expression, used by the list and by the hero, so they cannot drift
apart again.
"""
from __future__ import annotations

import pytest

from app.models import BatchType, ImportBatch, PropertyForSale


def _batch(db):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="week.csv", is_active=True, status="published")
    db.add(b)
    db.flush()
    return b


def _listing(db, batch, **kw):
    # is_held=False and a floor area are what _hide_bad_data requires of a row
    # before it is allowed in front of anyone — a held or incomplete row is
    # never in a deal list, so a fixture without them tests an empty page.
    row = PropertyForSale(import_batch_id=batch.id, suburb="Riverhead",
                          property_type="House", is_held=False,
                          floor_area_m2=150.0, land_area_m2=800.0,
                          cv_numeric=700_000.0, **kw)
    db.add(row)
    return row


@pytest.fixture()
def seeded(db_session):
    """Three listings where money and percentage disagree, deliberately.

    small   asking   600,000  fair   744,000   margin 24%   gain $144,000
    big     asking 3,000,000  fair 3,360,000   margin 12%   gain $360,000

    Sorted on the percentage, `small` wins. Sorted on the money — which is the
    number anyone is actually choosing between — `big` does.
    """
    b = _batch(db_session)
    small = _listing(db_session, b, address="1 Small Street",
                     asking_price=600_000.0, fair_value=744_000.0, margin=0.24,
                     best_net_gain=10_000.0, max_addl_lots=4,
                     confidence="high", comps_used=9)
    big = _listing(db_session, b, address="2 Big Boulevard",
                   asking_price=3_000_000.0, fair_value=3_360_000.0, margin=0.12,
                   best_net_gain=900_000.0, max_addl_lots=1,
                   confidence="high", comps_used=9)
    db_session.commit()
    return b, small, big


# ---- the hero and the list agree -------------------------------------------
@pytest.mark.parametrize("order_by", ["best_net_gain", "margin_dollars",
                                      "margin", "max_addl_lots"])
def test_the_headline_card_is_the_top_of_the_list(client, seeded, order_by):
    """Whatever a page is sorted by, the card above it is that page's first row.
    Asserted across every axis a page actually uses, because the fault was
    precisely an axis nobody had thought about."""
    lst = client.get(f"/api/properties?order_by={order_by}&order_dir=desc").json()
    summary = client.get(f"/api/properties/summary?order_by={order_by}").json()

    assert lst["rows"], "no rows came back"
    assert summary["top_id"] == lst["rows"][0]["id"], (
        f"sorted by {order_by}, the headline card is not the first row")


def test_the_subdividable_page_is_headed_by_its_biggest_net_gain(client, seeded):
    """THE BUG, named. best_net_gain fell through the old two-case guess and the
    page was headed by its biggest underpricing instead."""
    _b, small, big = seeded
    summary = client.get("/api/properties/summary?order_by=best_net_gain").json()

    assert summary["top_id"] == big.id, (
        "the subdividable headline is not the biggest net gain — it is almost "
        "certainly the biggest margin, which is the old fault")


# ---- money, not percentage --------------------------------------------------
def test_ranking_on_money_puts_the_bigger_cheque_first(client, seeded):
    _b, small, big = seeded
    rows = client.get("/api/properties?order_by=margin_dollars&order_dir=desc").json()["rows"]
    assert [r["id"] for r in rows][:2] == [big.id, small.id]


def test_ranking_on_the_percentage_still_works_and_disagrees(client, seeded):
    """The counterweight — proof the fixture really does separate the two, so
    the test above is measuring something."""
    _b, small, big = seeded
    rows = client.get("/api/properties?order_by=margin&order_dir=desc").json()["rows"]
    assert [r["id"] for r in rows][:2] == [small.id, big.id]


def test_the_underpriced_page_asks_for_money(client):
    """The page's own choice, asserted where it is made. The API can sort either
    way; which one the page asks for is the decision."""
    from pathlib import Path

    page = Path(__file__).resolve().parents[2] / "ollie-v5-frontend" / "app" / "underpriced" / "page.tsx"
    if not page.exists():
        pytest.skip("frontend not alongside this checkout")
    assert 'orderBy="margin_dollars"' in page.read_text(), \
        "the underpriced page is still ranked on the percentage"


# ---- one rule, one place ----------------------------------------------------
def test_both_endpoints_use_the_same_sort_expression():
    """The fault was two rules for one idea. Asserted structurally so a future
    change to one cannot silently leave the other behind."""
    import inspect

    from app.routers import properties as P

    src = inspect.getsource(P)
    assert src.count("def _sort_expression") == 1, "there is more than one sort rule again"
    assert inspect.getsource(P.summarise_for_sale).count("_sort_expression") >= 1, \
        "the headline card does not use the shared sort rule"
    assert inspect.getsource(P.list_for_sale).count("_sort_expression") >= 1, \
        "the row list does not use the shared sort rule"


def test_the_summary_refuses_an_axis_the_list_would_refuse(client):
    """The two took different validation, so the summary could be asked for a
    column the list rejects — and getattr on a bad name is a 500, not a 422."""
    r = client.get("/api/properties/summary?order_by=drop_table")
    assert r.status_code == 422, r.text


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import current_user, require_active, require_admin

    user = User(email="sort-test@test.local", password_hash="x",
                role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(user)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: user,
        require_active: lambda: user,
        require_admin: lambda: user,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}
