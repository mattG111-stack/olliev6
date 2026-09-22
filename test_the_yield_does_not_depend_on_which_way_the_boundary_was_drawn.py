"""The same section must take the same number of terraces whichever way round
its boundary happens to be stored.

Found while drawing an example property rather than by any test here. A 609 m²
section came back as three terraces one way up and NONE mirrored north for
south — identical shape, identical area, identical everything a person would
think to check.

WHY, AND IT IS A QUIET ONE. The packer rotates the parcel so the candidate
street edge lies along the x axis, then anchors its rows at the bottom of the
bounding box. Where the interior lands after that rotation — above the axis or
below it — decides whether the rows start at the STREET or at the REAR
boundary. And which side the interior is on IS the winding order of the ring.

A boundary can be digitised either way round and both are valid. So the answer
turned on a property of the record rather than of the land, and only one row
fits in 33 m of depth: anchored at the rear it lands on the tapered back of the
site instead of the wide street end. Both results read as findings about the
shape, and a developer would have believed whichever one they were shown.
"""
from __future__ import annotations

import math

import pytest

from site_layout import LOT_DEPTH_M, LOT_WIDTH_M, terrace_layout

LAT, LNG = -36.90210, 174.86000
MY = 110574.0
MX = 111320.0 * math.cos(math.radians(LAT))


def at(north_m: float, east_m: float) -> list[float]:
    return [LAT + north_m / MY, LNG + east_m / MX]


def closed(pts: list[list[float]]) -> list[list[float]]:
    return pts + [pts[0]]


# The real one: a section running south off the street, rear boundary splayed.
SITE = [at(2.0, -9.6), at(2.0, 9.6), at(-27.5, 11.4), at(-31.0, -8.2)]
# The same shape mirrored north for south. Same area, same side lengths.
MIRRORED = [[2 * LAT - p[0], p[1]] for p in SITE]


def fits(ring: list[list[float]], want: int = 3) -> int:
    return terrace_layout(closed(ring), want, origin=(LAT, LNG)).fits


# ---- the bug -----------------------------------------------------------------
def test_reversing_the_ring_does_not_change_the_yield():
    """THE ONE THAT WAS WRONG. Both windings describe the same land."""
    assert fits(SITE) == fits(SITE[::-1])


def test_mirroring_the_site_does_not_change_the_yield():
    assert fits(SITE) == fits(MIRRORED)


def test_all_four_windings_of_the_same_land_agree():
    got = {fits(r) for r in (SITE, SITE[::-1], MIRRORED, MIRRORED[::-1])}
    assert len(got) == 1, f"four descriptions of one section gave {got}"


def test_it_is_not_agreeing_on_zero():
    """A packer that returns nothing for everything is consistent and useless."""
    assert fits(SITE) > 0


# ---- and it still answers the question it exists to answer --------------------
def test_a_rectangle_that_plainly_takes_lots_still_does():
    box = [at(0, 0), at(0, 40), at(-60, 40), at(-60, 0)]
    assert fits(box, want=6) >= 4, "a 40 x 60 m rectangle should take several"


def test_the_shape_can_still_come_up_short_of_the_area():
    """The whole point of packing rather than dividing: a long thin site has
    the area for lots it cannot actually hold."""
    sliver = [at(0, 0), at(0, 7.0), at(-90, 7.0), at(-90, 0)]
    lay = terrace_layout(closed(sliver), 3, origin=(LAT, LNG))
    assert lay.fits < lay.by_area and lay.shape_limited


def test_a_site_too_small_for_one_lot_takes_none():
    tiny = [at(0, 0), at(0, LOT_WIDTH_M - 2), at(-(LOT_DEPTH_M - 4), LOT_WIDTH_M - 2),
            at(-(LOT_DEPTH_M - 4), 0)]
    assert fits(tiny, want=2) == 0


def test_the_lots_that_come_back_are_inside_the_section():
    """Consistency is not enough — they have to be on the land. A lot outside
    the boundary draws perfectly well and is not a lot."""
    from site_layout import _ring_metres

    lay = terrace_layout(closed(SITE), 3, origin=(LAT, LNG))
    poly = _ring_metres(closed(SITE), (LAT, LNG))

    def inside(pt) -> bool:
        x, y = pt
        hit = False
        for a, b in zip(poly, poly[1:] + poly[:1]):
            if (a[1] > y) != (b[1] > y):
                cross = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                if x < cross:
                    hit = not hit
        return hit

    assert lay.lots
    for lot in lay.lots:
        for corner in lot.corners:
            assert inside(corner), f"lot corner {corner} is outside the section"
