"""The mains on one house, asked for directly.

    "what are you on about the point of the api is it shows the water and
     waste water pipes"

Right, and the first cut had the wrong shape. It downloaded a region's whole
network into our database so distances could be measured against it later —
which is what you need when the source is a file somebody exports, and is a
detour when the source is a service. A property page has a point and a radius.
The service answers exactly that question in one request: nothing to load first,
and the answer is current rather than as current as the last import.

THE FAILURE THIS IS MOSTLY WRITTEN AGAINST. Every way an envelope query goes
wrong returns an EMPTY ANSWER WITH NO ERROR ON IT, and an empty answer reads on
screen as "no services near this property" — a servicing finding about the house
rather than a fault in our query. Send the box without `inSR` and the server
reads -36.9 as NZTM metres, which is a box in the Southern Ocean. Forget the
cosine and the box is a fifth too narrow east-west at Auckland's latitude, so a
main across the road is missed. Neither raises.
"""
from __future__ import annotations

import json

import pytest

from app.arcgis import fetch_near

LAT, LNG = -36.9021, 174.8600


def _params(u: str) -> dict:
    import urllib.parse

    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))


def _main(oid: int, lat: float = LAT, lng: float = LNG, **props) -> dict:
    return {"type": "Feature",
            "properties": {"OBJECTID": oid, **props},
            "geometry": {"type": "LineString",
                         "coordinates": [[lng, lat], [lng + 0.0003, lat]]}}


class Service:
    """A layer that only answers within the envelope it is given."""

    def __init__(self, features: list[dict], *, capped: bool = False):
        self.features, self.capped = features, capped
        self.seen: list[dict] = []

    def __call__(self, url: str) -> str:
        p = self.seen and None
        p = _params(url)
        self.seen.append(p)
        xmin, ymin, xmax, ymax = (float(v) for v in p["geometry"].split(","))
        inside = [f for f in self.features
                  if any(xmin <= x <= xmax and ymin <= y <= ymax
                         for x, y in f["geometry"]["coordinates"])]
        return json.dumps({"type": "FeatureCollection", "features": inside,
                           "exceededTransferLimit": self.capped})


# ---- it asks about the property, not the city --------------------------------
def test_one_request_returns_the_mains_around_this_house():
    srv = Service([_main(1), _main(2, lat=LAT + 0.0005)])
    got = fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250, get=srv)
    assert got.complete, got.note
    assert len(got.features) == 2
    assert len(srv.seen) == 1, "a property page should cost one request"


def test_a_main_a_suburb_away_is_not_returned():
    far = _main(9, lat=LAT + 0.05, lng=LNG + 0.05)
    got = fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250,
                     get=Service([_main(1), far]))
    assert [f["properties"]["OBJECTID"] for f in got.features] == [1]


def test_nothing_needs_to_be_loaded_first():
    """The whole point. No import, no database, no distances job — a point and
    a radius is the entire input."""
    got = fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250,
                     get=Service([_main(1)]))
    assert got.features


# ---- the silent empty answers ------------------------------------------------
def test_the_envelope_says_which_coordinates_it_is_in():
    """WITHOUT inSR THE SERVER READS THE BOX IN THE LAYER'S OWN PROJECTION.
    -36.9 as NZTM metres is a box in the Southern Ocean holding nothing — an
    empty answer with no error on it, which reads as a house with no services
    rather than a query we got wrong."""
    srv = Service([_main(1)])
    fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250, get=srv)
    assert srv.seen[0].get("inSR") == "4326"
    assert srv.seen[0].get("outSR") == "4326"


def test_the_box_is_widened_for_longitude_at_this_latitude():
    """A degree of longitude is about 89 km at Auckland, not 111. Using the
    latitude figure for both makes the box a fifth too narrow east-west, and a
    main across the road goes unfound with nothing to say so."""
    srv = Service([])
    fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250, get=srv)
    xmin, ymin, xmax, ymax = (float(v) for v in srv.seen[0]["geometry"].split(","))
    assert (xmax - xmin) > (ymax - ymin) * 1.15, "box not widened for longitude"


def test_the_box_actually_reaches_the_radius_asked_for():
    """A main at 240 m must be inside a 250 m box. Getting the metres-per-degree
    the wrong way round shrinks the box by a factor of ten and still returns a
    plausible-looking handful of pipes."""
    import math

    at_240_north = _main(1, lat=LAT + 240 / 110_574.0)
    at_240_east = _main(2, lng=LNG + 240 / (111_320.0 * math.cos(math.radians(LAT))))
    got = fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250,
                     get=Service([at_240_north, at_240_east]))
    assert len(got.features) == 2, "the box does not reach 240 m"


def test_the_spatial_filter_is_actually_sent():
    """Omit it and the server answers with the first page of the WHOLE layer,
    which is not an error and not this property."""
    srv = Service([_main(1)])
    fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250, get=srv)
    assert srv.seen[0]["geometryType"] == "esriGeometryEnvelope"
    assert srv.seen[0]["spatialRel"] == "esriSpatialRelIntersects"


def test_a_capped_answer_is_not_called_complete():
    """Too large a radius and the server hands back what it will and flags it.
    The far mains are missing, so the nearest-distance figure may be wrong."""
    got = fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250,
                     get=Service([_main(1)], capped=True))
    assert not got.complete
    assert "capped" in got.note


def test_a_projected_answer_is_refused_rather_than_drawn():
    def get(url: str) -> str:
        return json.dumps({
            "spatialReference": {"wkid": 2193},
            "features": [{"attributes": {"OBJECTID": 1},
                          "geometry": {"paths": [[[1757000.0, 5920000.0],
                                                  [1757010.0, 5920010.0]]]}}]})

    got = fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250, get=get)
    assert not got.complete and got.features == []
    assert "Tasman" in got.note


def test_a_service_that_is_down_does_not_raise():
    def get(url: str) -> str:
        raise OSError("connection reset")

    got = fetch_near("https://x/FeatureServer/0/query", LAT, LNG, 250, get=get)
    assert not got.complete and got.features == []
    assert "Could not reach" in got.note


# ---- and the same rules apply to what comes back -----------------------------
def test_a_retired_main_is_no_more_a_connection_live_than_loaded():
    """ONE RULE, TWO CALLERS. If the live path did its own parsing, a retired
    main would be excluded from the search and drawn on the map — the picture
    contradicting the number printed beside it."""
    from app.services_network import mains_from_features

    feats = [_main(1, **{"Asset Status": "DECM", "Asset Owner": "WATERCARE"}),
             _main(2, **{"Asset Status": "LIVE", "Asset Owner": "WATERCARE"})]
    got = mains_from_features(feats, "water")
    assert [m.external_id for m in got] == ["2"]


def test_a_road_drain_is_excluded_on_the_live_path_too():
    from app.services_network import mains_from_features

    feats = [_main(1, **{"Asset Owner": "TRANSPORT"}),
             _main(2, **{"Asset Owner": "WATERCARE"})]
    assert len(mains_from_features(feats, "water")) == 1


def test_the_live_path_reads_the_operators_field_names():
    from app.services_network import mains_from_features

    got = mains_from_features(
        [_main(1, **{"Asset Diameter (mm)": 150, "Pipe Material": "PVC",
                     "Asset Owner": "WATERCARE"})], "water")
    assert got[0].diameter_mm == 150 and got[0].material == "PVC"


def test_a_manhole_from_the_service_is_marked_as_a_node():
    """A node arrives as two identical vertices so one distance routine serves
    both. Drawn as a line it has zero length and is invisible — and it is the
    thing that matters most on a plan."""
    from app.services_network import mains_from_features

    pt = {"type": "Feature", "properties": {"OBJECTID": 5},
          "geometry": {"type": "Point", "coordinates": [LNG, LAT]}}
    got = mains_from_features([pt], "wastewater")
    assert got and got[0].is_node is True


# ---- the endpoint prefers what is loaded, and falls back otherwise -----------
def _services(db, monkeypatch, live):
    from app.routers import geo

    monkeypatch.setattr(geo, "_live_mains", lambda *a, **k: (live, False))
    return geo.services(lat=LAT, lng=LNG, radius_m=250.0, region="Auckland",
                        db=db, user=None)


def test_the_property_page_falls_back_to_the_service(db_session, monkeypatch):
    from app.services_network import Main

    live = [Main(kind="water", path=[[LAT, LNG], [LAT, LNG + 0.0003]],
                 owner="WATERCARE", diameter_mm=150, material="PVC")]
    got = _services(db_session, monkeypatch, live)
    assert [l.kind for l in got.lines] == ["water"]
    assert got.nearest["water"] == 0.0
    assert "Watercare" in got.attribution and got.caution


def test_what_is_loaded_wins_over_the_service(db_session, monkeypatch):
    """A loaded network is the whole region and a live answer is one box. When
    both exist the loaded one is the better answer and costs no request."""
    from app.models import ServicePipe
    from app.services_network import Main

    db_session.add(ServicePipe(
        region="Auckland", kind="stormwater", owner="STORMWATER",
        path=json.dumps([[LAT, LNG], [LAT, LNG + 0.0003]])))
    db_session.commit()
    called = []

    from app.routers import geo
    monkeypatch.setattr(geo, "_live_mains",
                        lambda *a, **k: (called.append(1) or [], False))
    got = geo.services(lat=LAT, lng=LNG, radius_m=250.0, region="Auckland",
                       db=db_session, user=None)
    assert [l.kind for l in got.lines] == ["stormwater"]
    assert called == [], "asked the service when we already had the network"


def test_a_property_with_nothing_either_way_says_so_plainly(db_session,
                                                            monkeypatch):
    got = _services(db_session, monkeypatch, [])
    assert got.lines == []
    assert got.note
    # No credit and no warning for pipes that are not on screen.
    assert got.attribution == "" and got.caution == ""
