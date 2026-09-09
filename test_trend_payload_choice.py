"""The same suburb must draw the same chart twice.

Every listing in a suburb carries its own copy of the portal's suburb aggregate,
scraped whenever that listing was scraped — so they disagree about how far they
reach. One stops at 2025, another carries 2026.

The chart used to take whichever of them the database happened to return first
from `ORDER BY <two flags> LIMIT 1`. Once those flags tie — and they tie for
every listing that has a yearly payload — that order is not stable between
executions. So the current year appeared on one page load and vanished on the
next, with the data unchanged, which is exactly how it was reported: "sometimes
displaying the 26 data sometimes not even tho when there is data there".

Choosing on content fixes both halves of that: it is stable, and it picks the
series that reaches furthest rather than an arbitrary one.
"""
from __future__ import annotations

import json

from app.routers.dashboards import _best_trend_payload, _trend_reach


def _yearly(years, monthly=None, ident=1):
    """(id, yearly_json, monthly_json) — the shape the query returns."""
    pts = [{"year": y, "median": 1_000_000 + y, "count": 20} for y in years]
    return (ident, json.dumps({"points": pts}), monthly)


def test_the_series_that_reaches_this_year_wins():
    stale = _yearly(range(2016, 2026), ident=11)     # ends 2025
    fresh = _yearly(range(2016, 2027), ident=97)     # carries 2026
    assert _best_trend_payload([stale, fresh])[0] == 97
    # ...and the answer does not depend on the order the rows arrive in.
    assert _best_trend_payload([fresh, stale])[0] == 97


def test_the_same_rows_always_choose_the_same_payload():
    """The whole fault: an arbitrary pick among ties."""
    rows = [_yearly(range(2016, 2026), ident=i) for i in (5, 2, 9, 7)]
    picks = {_best_trend_payload(list(reversed(rows)))[0] for _ in range(3)}
    picks |= {_best_trend_payload(rows)[0] for _ in range(3)}
    assert len(picks) == 1, f"the same data chose different payloads: {picks}"
    assert picks == {2}, "ties should settle on the lowest id"


def test_a_longer_history_wins_when_both_reach_the_same_year():
    short = _yearly(range(2023, 2027), ident=3)
    long_ = _yearly(range(2000, 2027), ident=44)
    assert _best_trend_payload([short, long_])[0] == 44


def test_a_corrupt_payload_never_beats_a_readable_one():
    broken = (8, "{not json at all", None)
    good = _yearly(range(2016, 2026), ident=12)
    assert _best_trend_payload([broken, good])[0] == 12
    assert _trend_reach("{not json at all") == (0, 0)
    assert _trend_reach(None) == (0, 0)


def test_a_monthly_only_payload_is_still_read():
    """Some listings carry only the monthly series; it still has a reach."""
    monthly = json.dumps({"points": [{"month": f"2026-{m:02d}", "median": 900_000}
                                     for m in range(1, 8)]})
    assert _trend_reach(monthly) == (2026, 7)
    only_monthly = (4, None, monthly)
    older_yearly = _yearly(range(2010, 2021), ident=6)
    # The yearly payload is preferred by the query's own ordering; here the point
    # is simply that a monthly-only row is readable rather than scoring zero.
    assert _best_trend_payload([only_monthly])[0] == 4
    assert _best_trend_payload([older_yearly, only_monthly])[0] == 6
