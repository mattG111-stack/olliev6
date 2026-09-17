"""Every year, on one line.

Remuera drew two points — 2025 and 2026 — because that is as far back as the
sold history loaded for it goes. Massey drew 2014 to 2025 from the older
suburb aggregate, and stopped before this year. Neither is what a reader asking
"how has this suburb moved" wants to see.

So the two are joined: our own sold records wherever we have them (they are the
suburb's actual transactions, and they carry the year in progress), and the
older aggregate for the years before ours begin.

The two sample the same market differently, so their medians for a shared year
rarely match to the dollar. Joining them raw would draw a step at the seam that
belongs to us rather than to the market, and refusing to join leaves a two-point
chart that answers nothing. So the older series is CHAIN-LINKED: scaled by the
ratio between the two on the year they share.

That preserves the shape — every year-on-year move in the older history
survives exactly — while the level is anchored to our own sales. It is what a
statistics agency does when rebasing a long series onto a new one, and it is the
only way to put a decade and this year on one line.
"""
from __future__ import annotations

import json

from app.routers.dashboards import _splice_years


def _series(pairs):
    return json.dumps({"points": [
        {"year": y, "median": m, "count": 20, "change_pct": 0.0} for y, m in pairs
    ]})


def _years(raw):
    return [p["year"] for p in json.loads(raw)["points"]]


def _by_year(raw):
    return {p["year"]: p["median"] for p in json.loads(raw)["points"]}


OURS = [(2025, 1_760_000), (2026, 2_060_000)]
THEIRS = [(y, 1_000_000 + (y - 2014) * 70_000) for y in range(2014, 2026)]


def test_the_line_covers_every_year_either_source_knows():
    """The Remuera case: two points becomes the full history plus this year."""
    merged = _splice_years(_series(OURS), _series(THEIRS))
    assert _years(merged) == list(range(2014, 2027))


def test_our_own_sales_win_on_a_year_both_hold():
    """Where we have the actual transactions, they are the better measure."""
    merged = _splice_years(_series(OURS), _series(THEIRS))
    assert _by_year(merged)[2025] == 1_760_000, "the aggregate overwrote our own year"
    assert _by_year(merged)[2026] == 2_060_000
    # The older years come through scaled onto our level. Here the two sources
    # nearly agree on 2025 (1.76M vs 1.77M), so the scaling is barely visible —
    # which is exactly what should happen when they already line up.
    assert 990_000 < _by_year(merged)[2014] < 1_010_000, _by_year(merged)[2014]


def test_the_change_is_recomputed_across_the_joined_line():
    """Each year's change has to describe the year before it ON THIS LINE."""
    merged = json.loads(_splice_years(_series(OURS), _series(THEIRS)))["points"]
    assert merged[0]["change_pct"] == 0.0
    for prev, cur in zip(merged, merged[1:]):
        want = round((cur["median"] - prev["median"]) / prev["median"] * 100, 1)
        assert cur["change_pct"] == want, cur


def test_a_source_at_a_different_level_is_scaled_onto_ours():
    """The Remuera case. Their 2025 is well below ours; the history still shows.

    A five-year line is not a history and two points are not a trend, so the
    older series is scaled to meet ours rather than being thrown away.
    """
    theirs = _series([(y, 400_000 + (y - 2014) * 20_000) for y in range(2014, 2026)])
    merged = _splice_years(_series(OURS), theirs)
    assert _years(merged) == list(range(2014, 2027))
    by_year = _by_year(merged)
    # Our own years are untouched...
    assert by_year[2025] == 1_760_000 and by_year[2026] == 2_060_000
    # ...and the year before the join sits just under it, not at a third of it.
    assert 1_500_000 < by_year[2024] < 1_760_000, by_year[2024]


def test_scaling_preserves_every_move_in_the_older_history():
    """The shape is the point; only the level is re-anchored."""
    raw = [(y, 400_000 + (y - 2014) * 20_000) for y in range(2014, 2026)]
    merged = _by_year(_splice_years(_series(OURS), _series(raw)))
    raw_by_year = dict(raw)
    for (y0, _), (y1, _) in zip(raw, raw[1:]):
        if y1 >= min(dict(OURS)):
            break
        theirs_move = raw_by_year[y1] / raw_by_year[y0]
        ours_move = merged[y1] / merged[y0]
        assert abs(ours_move - theirs_move) < 0.001, (
            f"{y0}->{y1} moved {ours_move:.4f} on the chart but {theirs_move:.4f} in the data"
        )


def test_the_joined_line_clears_the_bar_for_a_useful_history():
    from app.routers.dashboards import _MIN_USEFUL_YEARS
    theirs = _series([(y, 400_000 + (y - 2014) * 20_000) for y in range(2014, 2026)])
    merged = _splice_years(_series(OURS), theirs)
    assert len(_years(merged)) > _MIN_USEFUL_YEARS


def test_a_suburb_with_no_sales_of_our_own_still_shows_the_history():
    assert _years(_splice_years(None, _series(THEIRS))) == list(range(2014, 2026))


def test_a_suburb_with_no_aggregate_still_shows_our_sales():
    assert _years(_splice_years(_series(OURS), None)) == [2025, 2026]


def test_an_aggregate_that_adds_nothing_older_is_left_out():
    """No point re-deriving the same years through a second source."""
    theirs = _series([(2025, 1_760_000), (2026, 2_000_000)])
    merged = _splice_years(_series(OURS), theirs)
    assert _by_year(merged) == {2025: 1_760_000, 2026: 2_060_000}


def test_corrupt_payloads_never_take_the_line_down():
    assert _splice_years("{not json", _series(THEIRS)) == _series(THEIRS)
    assert _years(_splice_years(_series(OURS), "{not json")) == [2025, 2026]
