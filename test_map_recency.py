"""2,950 sold dots over one suburb is a texture, not a market.

The trends map plotted every sale a suburb had ever recorded. In Flat Bush that
is 2,950 points: every street solid grey, and the 88 live listings you opened
the page to look at buried underneath them.

Sold points now reach back six months by default, with 3 / 6 / 12 / 24 / all
offered on the map itself.

The awkward part is that sold_date is a STRING and the feed carries two formats
— "5/14/2026" from one export and "2026-05-14" from another — so the window
cannot be a SQL date comparison. What SQL CAN do is the year, which appears
verbatim in both, and that narrows a region-wide query before it is fetched; the
exact month test then runs in Python over a small set. These tests pin both
halves, because a year-only filter that was never followed by the month test
would quietly return eighteen months of sales and look fine.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.routers.properties import (
    SOLD_MAP_MONTHS,
    _in_months,
    _recent_months,
)

AUG_2026 = datetime(2026, 8, 22, tzinfo=timezone.utc)


# ---- the window ------------------------------------------------------------
def test_six_months_means_six_months_including_this_one():
    m = _recent_months(6, AUG_2026)
    assert sorted(m) == ["2026-03", "2026-04", "2026-05", "2026-06",
                         "2026-07", "2026-08"]


def test_the_default_is_six():
    assert SOLD_MAP_MONTHS == 6


@pytest.mark.parametrize("months", [3, 6, 12, 24])
def test_every_range_the_map_offers_returns_that_many_months(months):
    assert len(_recent_months(months, AUG_2026)) == months


def test_the_window_crosses_a_year_boundary():
    m = _recent_months(6, datetime(2026, 2, 10, tzinfo=timezone.utc))
    assert sorted(m) == ["2025-09", "2025-10", "2025-11", "2025-12",
                         "2026-01", "2026-02"]


def test_the_window_is_anchored_on_today_not_on_the_newest_sale():
    """If the feed is a fortnight stale, "last 6 months" should show five and a
    half months of sales. Sliding the window back to wherever the data happens
    to end would hide exactly the gap worth noticing."""
    m = _recent_months(6, AUG_2026)
    assert "2026-08" in m, "the window must reach the month we are in"


# ---- both date formats in the same column ----------------------------------
@pytest.mark.parametrize("sold_date", ["2026-08-04", "8/4/2026", "2026-03-01"])
def test_a_sale_inside_the_window_is_kept_whichever_format_it_is_in(sold_date):
    assert _in_months(sold_date, _recent_months(6, AUG_2026))


@pytest.mark.parametrize("sold_date", ["2026-02-28", "1/2/2025", "2019-06-30"])
def test_a_sale_outside_the_window_is_dropped(sold_date):
    assert not _in_months(sold_date, _recent_months(6, AUG_2026))


def test_the_boundary_month_is_in_and_the_one_before_it_is_out():
    m = _recent_months(6, AUG_2026)
    assert _in_months("2026-03-31", m)
    assert not _in_months("2026-02-28", m)


@pytest.mark.parametrize("bad", [None, "", "rubbish", "not a date", "0"])
def test_an_unreadable_date_is_dropped_rather_than_shown(bad):
    """A sale we cannot date cannot be placed in time, so it does not belong on
    a map that is claiming to show the last six months."""
    assert not _in_months(bad, _recent_months(6, AUG_2026))


def test_the_year_prefilter_cannot_stand_in_for_the_month_test():
    """The SQL half narrows by YEAR, which is what makes a region-wide query
    affordable. On its own it is far too loose — every one of these shares a
    year with the window and none of them is inside it."""
    m = _recent_months(6, AUG_2026)              # Mar-Aug 2026
    for same_year_but_outside in ("2026-01-15", "2026-02-01", "1/20/2026"):
        assert not _in_months(same_year_but_outside, m), same_year_but_outside


# ---- the endpoint -----------------------------------------------------------
def test_the_map_endpoint_defaults_to_the_six_month_window(db_session):
    """Called with no window at all, the map must not go back to the beginning
    of time — that default is the whole bug."""
    import inspect

    from app.routers.properties import map_points

    sig = inspect.signature(map_points)
    assert "sold_months" in sig.parameters
    assert sig.parameters["sold_months"].default.default == SOLD_MAP_MONTHS


def test_for_sale_points_are_not_filtered_by_a_sold_window(db_session):
    """Every live listing is live by definition; a sold window must not touch
    them or the map would start hiding the listings it exists to show."""
    from app.routers.properties import map_points

    res = map_points(dataset="for_sale", region="Auckland", sold_months=3,
                     db=db_session)
    assert res.dataset == "for_sale"        # ran without error, nothing dropped


def test_none_means_every_sale_on_record(db_session):
    """Direct callers can ask for the lot — used by the batch-accumulation test
    in test_sold_counts_agree.py, which is about a different question."""
    from app.routers.properties import map_points

    res = map_points(dataset="sold", region="Auckland", sold_months=None,
                     db=db_session)
    assert res.dataset == "sold"


def test_an_unpassed_query_default_is_not_treated_as_a_number(db_session):
    """Called as a plain function, an unpassed Query(6) arrives as the Query
    OBJECT — truthy, and fatal the moment it meets arithmetic. This endpoint's
    `limit` has been bitten by exactly that before."""
    from app.routers.properties import map_points

    res = map_points(dataset="sold", region="Auckland", db=db_session)
    assert res.dataset == "sold"       # would have raised TypeError
