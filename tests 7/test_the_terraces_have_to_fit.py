"""Six terraces by arithmetic is not six terraces on the ground.

    "it woul be cool to try do a muck up on each site"

The yield is land area, less a share for shared access, divided by the land one
terrace needs. It ranks a thousand sites well and it cannot see shape. Four
sections of 980 m² — a wedge, a rectangle, a long thin strip and a square — all
return the same six, and they do not hold the same number of houses.

So the layout packs the real boundary with rectangles the size the yield
assumes, and counts the ones that land inside it. The number it returns can be
smaller than the arithmetic's and must never be larger: this exists to catch a
site that looked better than it is, not to talk one up.

The tests below are mostly about the ways a packer lies. It can count a house
that hangs over a boundary, count one straddling the notch of an L-shaped site,
give up at the narrow end of a wedge and report the whole section as unusable,
or — the one that actually happened while writing it — refuse a perfectly
rectangular site because the first rectangle it tried sat one centimetre outside
the line.
"""
from __future__ import annotations

import math

import pytest

from site_layout import (
    ACCESS_WIDTH_M, BOUNDARY_CLEAR_M, LOT_DEPTH_M, LOT_WIDTH_M, MAX_LOTS,
    terrace_layout,
)
from pricing.subdivision import THAB_LOT_M2

LAT, LNG = -36.9012, 174.8821
_MY = 110574.0
_MX = 111320.0 * math.cos(math.radians(LAT))


def ring(pts: list[tuple[float, float]]) -> list[list[float]]:
    """Local metres (east, north) as a closed [[lat, lng], ...] ring."""
    r = [[LAT + n / _MY, LNG + e / _MX] for e, n in pts]
    return r + [r[0]]


def box(w: float, d: float) -> list[list[float]]:
    return ring([(0, 0), (w, 0), (w, d), (0, d)])


# ---- the lot is the same lot the money is counted on -------------------------
def test_the_footprint_matches_the_yield_it_is_testing():
    """A layout drawn to a different lot size than the yield was costed on
    would disagree with the pro-forma beside it, and the drawing would be the
    one believed — it looks like evidence."""
    assert LOT_WIDTH_M * LOT_DEPTH_M == pytest.approx(THAB_LOT_M2)


# ---- shape is the whole point ------------------------------------------------
def test_the_same_area_in_four_shapes_is_four_different_answers():
    """THE ONE THIS EXISTS FOR. Identical land area, and the arithmetic cannot
    tell them apart."""
    wedge = terrace_layout(ring([(0, 0), (49.8, 0), (49.8, 9.4), (-0.158, 29.9)]), 6)
    rect = terrace_layout(box(35, 28), 6)
    strip = terrace_layout(box(70, 14), 6)
    square = terrace_layout(box(31.3, 31.3), 6)

    got = {wedge.fits, rect.fits, strip.fits, square.fits}
    assert len(got) > 1, (
        f"every shape returned the same count {got} — the packing is not "
        f"looking at the boundary")
    assert wedge.fits < 6 and wedge.shape_limited


def test_a_rectangular_site_is_not_reported_as_unusable():
    """The bug that turned up on the first run. A 35 m by 28 m section takes
    five of these comfortably; it came back with none, because the first test
    rectangle was placed hard on the boundary and inflated outwards from
    there. A zero here reads as "this site cannot be developed"."""
    got = terrace_layout(box(35, 28), 6)
    assert got.fits >= 4, f"a plain rectangle returned {got.fits} lots"


def test_a_frontage_of_exactly_two_lots_takes_two():
    """14 m is two 6 m lots and two 1 m margins, to the centimetre. The
    tolerance that lifts the test off the boundary line must not be charged to
    the width, or every exact fit comes back one short."""
    assert LOT_WIDTH_M * 2 + BOUNDARY_CLEAR_M * 2 == pytest.approx(14.0)
    assert terrace_layout(box(14, 22), 2).fits == 2


def test_the_narrow_end_of_a_wedge_does_not_condemn_the_wide_end():
    """A wedge is unusable at one end and fine at the other. A packer that
    stops at its first failure reports the narrow end as the whole site."""
    got = terrace_layout(ring([(0, 0), (49.8, 0), (49.8, 9.4), (-0.158, 29.9)]), 6)
    assert got.fits >= 2


# ---- the ways a packer cheats ------------------------------------------------
def test_nothing_is_placed_over_a_boundary():
    """Every corner of every lot, back in metres, inside the parcel.

    Measured from the box's own corner rather than the parcel centroid, which
    is the whole reason `origin` exists — see the next test.
    """
    got = terrace_layout(box(40, 30), 6, origin=(LAT, LNG))
    assert got.fits > 0
    for lot in got.lots:
        for x, y in lot.corners:
            assert -1e-6 <= x <= 40.0 + 1e-6
            assert -1e-6 <= y <= 30.0 + 1e-6


def test_the_lots_come_back_measured_from_where_they_were_asked_for():
    """The editor places buildings as metres from the LISTING'S pin. A layout
    measured from the parcel's own centroid arrives correct in shape and wrong
    in position — every house shifted by the gap between the pin and the middle
    of the section, with nothing on screen to say so."""
    from_corner = terrace_layout(box(40, 30), 6, origin=(LAT, LNG))
    from_centre = terrace_layout(box(40, 30), 6)
    assert from_corner.fits == from_centre.fits

    first_corner = min(x for x, _ in from_corner.lots[0].corners)
    first_centre = min(x for x, _ in from_centre.lots[0].corners)
    # The centroid frame puts the same lot ~20 m west of the corner frame.
    assert first_corner - first_centre == pytest.approx(20.0, abs=0.5)


def test_nothing_straddles_the_notch_of_an_l_shaped_site():
    """An L-shaped parcel — a right of way cut out of one corner. A rectangle
    can have all four corners inside an L while its middle crosses the missing
    piece, so corner tests alone put a house on the neighbour's land."""
    ell = ring([(0, 0), (40, 0), (40, 40), (22, 40), (22, 18), (0, 18)])
    got = terrace_layout(ell, 8)
    missing = [(x, y) for lot in got.lots for x, y in lot.corners
               if x > 22.0 + 1e-6 and y > 18.0 + 1e-6]
    assert missing == [], f"{len(missing)} corner(s) landed in the cut-out"


def test_two_lots_never_overlap():
    got = terrace_layout(box(60, 40), 14)
    assert got.fits >= 2
    rects = [(min(x for x, _ in l.corners), min(y for _, y in l.corners),
              max(x for x, _ in l.corners), max(y for _, y in l.corners))
             for l in got.lots]
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            apart = (a[2] <= b[0] + 1e-6 or b[2] <= a[0] + 1e-6
                     or a[3] <= b[1] + 1e-6 or b[3] <= a[1] + 1e-6)
            assert apart, f"{a} overlaps {b}"


def test_rows_are_separated_by_a_driveway():
    """Two rows back to back with no access between them is a drawing of
    something nobody can reach."""
    got = terrace_layout(box(20, 60), 6)
    ys = sorted({round(min(y for _, y in l.corners), 2) for l in got.lots})
    if len(ys) > 1:
        assert ys[1] - ys[0] >= LOT_DEPTH_M + ACCESS_WIDTH_M - 1e-6


# ---- it must never talk a site up --------------------------------------------
def test_it_never_returns_more_than_the_yield_asked_for():
    """A packing that finds room for more is not licence to raise the yield.
    The area model carries the access share and the practical cap, and this
    answers one question: does the shape take the number we quoted."""
    roomy = terrace_layout(box(80, 60), 3)
    assert roomy.fits == 3
    assert roomy.shape_limited is False
    assert len(roomy.lots) == 3


def test_it_stops_at_the_practical_cap():
    got = terrace_layout(box(200, 200), 500)
    assert got.fits <= MAX_LOTS


# ---- nothing to draw is nothing to draw --------------------------------------
def test_a_site_with_no_boundary_reports_nothing():
    got = terrace_layout([], 6)
    assert got.fits == 0 and got.lots == [] and got.street_side is None


def test_a_site_the_yield_rejected_is_not_drawn_anyway():
    """Zero terraces means zero terraces. Drawing one because a rectangle
    happens to fit would contradict the page it sits on."""
    got = terrace_layout(box(40, 30), 0)
    assert got.fits == 0 and got.lots == []


def test_a_section_too_small_for_one_lot_fits_none():
    assert terrace_layout(box(6, 6), 2).fits == 0
