"""What you can see is what the map is showing, not a circle round the pin.

    "It should look like that"  — pipes running across every street in view,
    on the basemap, the way the council's own viewer draws them.

A FIXED RADIUS ENDS MID-ROAD. The main running past the house stops dead a few
doors down, and that gap does not read as "the edge of what we asked for" — it
reads as missing data, on a drawing whose whole job is to say where the pipes
are. So the view's own bounds are the query, and moving the map asks again.

TWO THINGS THAT MUST NOT FOLLOW FROM THAT. A map zooms out to the whole
country, and "every pipe in New Zealand" is a request that times out for us and
hurts the operator; past a sane width the window stays around the property
rather than refusing, because a developer who zooms out wants context and an
error is not context. And the distances must keep meaning what they say:
panning changes what is DRAWN, never what "the nearest main" is.
"""
from __future__ import annotations

import json
import math

import pytest

from routers.geo import MAX_VIEW_M, _view_box

LAT, LNG = -36.9021, 174.8600


def span_m(box) -> tuple[float, float]:
    w, s, e, n = box
    return ((e - w) * 111_320.0 * math.cos(math.radians(LAT)),
            (n - s) * 110_574.0)


def around(half_m: float):
    dlat = half_m / 110_574.0
    dlng = half_m / (111_320.0 * math.cos(math.radians(LAT)))
    return (LAT + dlat, LAT - dlat, LNG + dlng, LNG - dlng)   # n, s, e, w


# ---- the view is the query ---------------------------------------------------
def test_the_view_becomes_the_envelope():
    n, s, e, w = around(400)
    box = _view_box(n, s, e, w, LAT, LNG, 250)
    ew, ns = span_m(box)
    assert 780 < ew < 820 and 780 < ns < 820


def test_a_view_wider_than_the_radius_is_not_narrowed_to_it():
    """THE BUG THIS EXISTS FOR. Clamping back to the 250 m default would leave
    the main stopping mid-road exactly as before, with a bounds parameter that
    did nothing."""
    n, s, e, w = around(900)
    ew, _ = span_m(_view_box(n, s, e, w, LAT, LNG, 250))
    assert ew > 1500


def test_no_bounds_means_no_envelope():
    assert _view_box(None, None, None, None, LAT, LNG, 250) is None


def test_three_sides_of_a_rectangle_is_not_a_rectangle():
    """Guessing the fourth would quietly query somewhere else."""
    n, s, e, _ = around(400)
    assert _view_box(n, s, e, None, LAT, LNG, 250) is None


def test_an_omitted_fastapi_parameter_is_not_a_number():
    """An omitted query parameter arrives as the Query object itself when the
    endpoint is called directly. A Query is not None, so an `is not None` check
    sails through it and the first comparison raises. Cost an hour once."""
    from fastapi import Query as Q

    q = Q(None)
    assert _view_box(q, q, q, q, LAT, LNG, 250) is None


def test_an_inverted_view_is_refused_rather_than_queried():
    """A negative-width envelope matches nothing, and an empty answer reads as
    a property with no services."""
    n, s, e, w = around(400)
    assert _view_box(s, n, e, w, LAT, LNG, 250) is None      # north below south
    assert _view_box(n, s, w, e, LAT, LNG, 250) is None      # east west of west


# ---- and it cannot become a request for the country --------------------------
def test_zooming_out_to_the_country_does_not_ask_for_the_country():
    n, s, e, w = around(80_000)
    ew, ns = span_m(_view_box(n, s, e, w, LAT, LNG, 250))
    assert ew <= MAX_VIEW_M * 2 + 1 and ns <= MAX_VIEW_M * 2 + 1


def test_a_clamped_view_still_covers_the_property():
    """Clamped, not refused. A developer who zooms out wants context, and the
    pipes by the site are the context — but the window has to still contain the
    site, or zooming out makes them vanish."""
    n, s, e, w = around(80_000)
    bw, bs, be, bn = _view_box(n, s, e, w, LAT, LNG, 250)
    assert bs < LAT < bn and bw < LNG < be


def test_the_clamp_keeps_the_view_centred_not_the_pin():
    """Panning away from the property and zooming out should clamp around what
    is on screen. Re-centring on the pin would snap the drawing back to a
    property the user has scrolled away from."""
    off_lat, off_lng = LAT + 0.02, LNG + 0.02
    dlat, dlng = 80_000 / 110_574.0, 80_000 / (111_320.0 * math.cos(math.radians(LAT)))
    box = _view_box(off_lat + dlat, off_lat - dlat, off_lng + dlng,
                    off_lng - dlng, LAT, LNG, 250)
    assert box[1] < off_lat < box[3]


# ---- the distance never depends on the view ----------------------------------
def _mains(kind: str, north_m: float):
    from services_network import Main

    dlat = north_m / 110_574.0
    dlng = 900 / (111_320.0 * math.cos(math.radians(LAT)))
    return Main(kind=kind, owner="WATERCARE",
                path=[[LAT + dlat, LNG - dlng], [LAT + dlat, LNG + dlng]],
                diameter_mm=150, material="PVC")


@pytest.mark.parametrize("half_m", [150, 400, 1200])
def test_the_nearest_distance_is_the_same_at_every_zoom(db_session, monkeypatch,
                                                        half_m):
    """The view decides what is DRAWN. If it also moved the distances, the same
    house would report a different servicing answer at every zoom level, and
    the number beside the map is the one a developer acts on."""
    from routers import geo

    monkeypatch.setattr(geo, "_live_mains",
                        lambda *a, **k: ([_mains("water", 40.0)], False))
    n, s, e, w = around(half_m)
    got = geo.services(lat=LAT, lng=LNG, radius_m=250.0, north=n, south=s,
                       east=e, west=w, region="Auckland", db=db_session,
                       user=None)
    assert got.nearest["water"] == pytest.approx(40.0, abs=0.6)


def test_a_main_outside_the_view_is_not_drawn(db_session, monkeypatch):
    """Otherwise the panel contradicts itself: a pipe listed in the response
    with no line anywhere on the map."""
    from routers import geo

    monkeypatch.setattr(geo, "_live_mains",
                        lambda *a, **k: ([_mains("water", 40.0),
                                          _mains("stormwater", 3000.0)], False))
    n, s, e, w = around(200)
    got = geo.services(lat=LAT, lng=LNG, radius_m=250.0, north=n, south=s,
                       east=e, west=w, region="Auckland", db=db_session,
                       user=None)
    assert [l.kind for l in got.lines] == ["water"]
    assert "stormwater" not in got.nearest


def test_a_wider_view_draws_more_of_the_network(db_session, monkeypatch):
    """The point of the change, stated as a test: zoom out, see more pipe."""
    from routers import geo

    monkeypatch.setattr(geo, "_live_mains",
                        lambda *a, **k: ([_mains("water", 40.0),
                                          _mains("wastewater", 700.0)], False))

    def count(half_m):
        n, s, e, w = around(half_m)
        return len(geo.services(lat=LAT, lng=LNG, radius_m=250.0, north=n,
                                south=s, east=e, west=w, region="Auckland",
                                db=db_session, user=None).lines)

    assert count(200) == 1
    assert count(900) == 2


def test_a_main_that_merely_passes_through_the_view_is_drawn(db_session,
                                                             monkeypatch):
    """THE ONE THAT WAS WRONG, AND IT HID BEHIND THE EXAMPLES. A main is one
    long polyline down a street, with its vertices wherever the surveyor put
    them — at the corners, hundreds of metres off. Asking whether a VERTEX is
    in view drops every pipe that merely PASSES THROUGH it, which is all the
    ones that matter: the main outside the house vanishes the moment you zoom
    in on the house, and the map looks like a street with no water."""
    from routers import geo

    dlat = 40.0 / 110_574.0
    far = 5_000 / (111_320.0 * math.cos(math.radians(LAT)))
    from services_network import Main

    long_main = Main(kind="water", owner="WATERCARE", diameter_mm=150,
                     path=[[LAT + dlat, LNG - far], [LAT + dlat, LNG + far]])
    monkeypatch.setattr(geo, "_live_mains", lambda *a, **k: ([long_main], False))

    n, s, e, w = around(120)              # zoomed right in on the house
    got = geo.services(lat=LAT, lng=LNG, radius_m=250.0, north=n, south=s,
                       east=e, west=w, region="Auckland", db=db_session,
                       user=None)
    assert [l.kind for l in got.lines] == ["water"], (
        "a main running past the property was dropped because its ends are "
        "5 km away")
    assert got.nearest["water"] == pytest.approx(40.0, abs=0.6)


def test_a_manhole_in_view_is_still_drawn(db_session, monkeypatch):
    """A node is two identical vertices. Whatever the crossing test does, it
    must not stop being a point-in-window question for these."""
    from routers import geo
    from services_network import Main

    dlat = 30.0 / 110_574.0
    node = Main(kind="wastewater", owner="WATERCARE", is_node=True,
                path=[[LAT + dlat, LNG], [LAT + dlat, LNG]])
    monkeypatch.setattr(geo, "_live_mains", lambda *a, **k: ([node], False))
    n, s, e, w = around(120)
    got = geo.services(lat=LAT, lng=LNG, radius_m=250.0, north=n, south=s,
                       east=e, west=w, region="Auckland", db=db_session,
                       user=None)
    assert len(got.lines) == 1 and got.lines[0].is_node


# ---- the envelope actually sent ----------------------------------------------
def test_the_service_is_asked_for_the_view_not_the_radius():
    from arcgis import fetch_envelope

    seen = {}

    def get(url: str) -> str:
        import urllib.parse
        seen.update(dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(url).query)))
        return json.dumps({"features": []})

    fetch_envelope("https://x/FeatureServer/0/query", 174.85, -36.91,
                   174.87, -36.89, get=get)
    assert seen["geometry"] == "174.85,-36.91,174.87,-36.89"
    assert seen["inSR"] == "4326"
    assert seen["geometryType"] == "esriGeometryEnvelope"
