"""How big is the section, and what shape.

    "we need to to get water pipes and waste water maping and off market
     property data so a developer can look for houses that are zoned right and
     the dimensions"

A developer's first question about a site is its dimensions, and the map has
drawn a length against each boundary for a while. That is where the numbers
stopped: on a canvas, in one panel. They were not in the API response, not in an
export, not filterable, and not available for a site nobody has listed. A filter
needs numbers as numbers.

The other half of this file is about a number that must NOT appear. Where LINZ
has no parcel, the panel draws a SQUARE of √(land area) so the shade has ground
to fall on, and it was labelling that square's sides exactly like a surveyed
boundary — four confident figures to one decimal place, every one of them a
consequence of assuming the section is square. A caption saying "approximated"
does not undo a number with an arrow pointing at it.
"""
from __future__ import annotations

import math

import pytest

from routers.geo import Edge, Parcel, _ring_edges

LAT, LNG = -36.9, 174.9


def _rect(width_m: float, depth_m: float, *, closed: bool = True) -> list[list[float]]:
    """A rectangle `width_m` east-west by `depth_m` north-south, as [lat, lng]."""
    dlat = depth_m / 110574.0
    dlng = width_m / (111320.0 * math.cos(math.radians(LAT)))
    ring = [[LAT, LNG], [LAT, LNG + dlng], [LAT + dlat, LNG + dlng], [LAT + dlat, LNG]]
    return ring + [[LAT, LNG]] if closed else ring


# ---- the measurement ---------------------------------------------------------
def test_every_side_is_measured():
    p = Parcel(source="linz", ring=_rect(20, 40))
    assert [e.length_m for e in p.edges] == [20.0, 40.0, 20.0, 40.0]


def test_it_says_which_way_each_side_runs():
    """A length alone does not describe a shape. Two sites with identical sides
    can be a rectangle and a rhombus, and only one of them takes a house."""
    p = Parcel(source="linz", ring=_rect(20, 40))
    assert [e.bearing_deg for e in p.edges] == [90.0, 0.0, 270.0, 180.0]


def test_the_closing_point_is_not_a_side():
    """A closed ring repeats its first point. Left in, the parcel gains a final
    boundary of zero metres — a "0.0 m" label on the map, and a nonsense
    minimum in any filter that looks at the shortest side."""
    closed = Parcel(source="linz", ring=_rect(20, 40, closed=True))
    open_ = Parcel(source="linz", ring=_rect(20, 40, closed=False))
    assert len(closed.edges) == 4
    assert [e.length_m for e in closed.edges] == [e.length_m for e in open_.edges]
    assert all(e.length_m > 0 for e in closed.edges)


def test_the_totals_are_there_without_adding_them_up():
    p = Parcel(source="linz", ring=_rect(20, 40))
    assert p.perimeter_m == 120.0
    assert p.longest_side_m == 40.0


def test_a_survey_sliver_is_not_a_side_of_the_section():
    """Real titles carry tiny chamfers where boundaries meet and kinks around
    easements. They are in the survey and they are not dimensions — counted as
    sides, every corner of the section looks like an extra boundary."""
    ring = _rect(20, 40, closed=False)
    # A 20 cm nick off one corner.
    ring.insert(1, [LAT, LNG + 0.2 / (111320.0 * math.cos(math.radians(LAT)))])
    p = Parcel(source="linz", ring=ring)
    assert all(e.length_m >= 0.5 for e in p.edges)
    assert len(p.edges) == 4


def test_a_parcel_we_do_not_have_reports_nothing_not_zero():
    p = Parcel(source="none")
    assert p.edges == []
    assert p.perimeter_m is None and p.longest_side_m is None


def test_a_line_is_not_a_section():
    assert _ring_edges([[LAT, LNG], [LAT + 0.0001, LNG]]) == []


# ---- the one that must not be reported ---------------------------------------
def test_the_longest_side_is_not_called_a_frontage():
    """Which side faces the road is a question about roads, and there is no road
    data here to answer it with. A field named `frontage_m` would be a guess
    wearing a measurement's clothes, and frontage is exactly the number a
    developer would act on."""
    assert not hasattr(Parcel(source="linz", ring=_rect(20, 40)), "frontage_m")
    assert "frontage" not in Parcel.model_fields


def test_the_estimated_square_carries_no_measurements():
    """THE ONE THAT MATTERS.

    Where LINZ has no parcel the panel still needs a shape, so it uses a square
    of √(land area). That square must never be measurable: its sides are not
    boundaries, they are the square root of one number, printed four times.

    Proved at the source — a parcel with no ring has no edges, so there is
    nothing for the map to label — and against the component, which must gate
    its labels on the boundary being the surveyed one rather than on a shape
    being present.
    """
    import pathlib

    assert Parcel(source="none", area_m2=800.0).edges == []

    sun = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "components" / "SunMap.tsx"
    if not sun.exists():
        pytest.skip("frontend not checked out beside the backend")
    src = sun.read_text(encoding="utf-8")
    assert 'const measured = parcel?.source === "linz"' in src, (
        "the map no longer distinguishes a surveyed boundary from the estimated "
        "square before labelling it")
    assert "for (const e of measured ? edges(section) : [])" in src, (
        "the estimated square is being labelled with lengths again")
