"""Stormwater, wastewater and water, and how far each is from a section.

    "we just need to add stormwater and water on sections"

The land says what a site could hold. This says whether it can be connected,
which is the question that stops a development after the land has been paid for.
A large, well-zoned, flat, nearly empty parcel with no wastewater within reach is
not a development site. It is a bill.

Three ways a servicing figure misleads, and all three are worse than having no
figure at all:

    A distance read as a permission. It is neither. Capacity, depth, the fall
    available and the operator's approval decide a connection, and none of them
    are in any published layer. So the fields are named for what they measure.

    "Not known" shown as "nothing nearby". A developer walks away from a site on
    that difference. Null has to stay null, and a filter must not quietly drop
    every parcel it cannot judge.

    The wrong projection. NZ network exports are routinely NZTM2000, and a
    projected coordinate is only a bigger number — nothing raises, and the whole
    network lands in the Tasman Sea looking like a clean load, with every
    distance in the region then computed against nothing.
"""
from __future__ import annotations

import math

import pytest

from app.models import LandParcel, ServicePipe
from app.parcels import load_parcels
from app.services_network import (
    MAX_SEARCH_M, build_networks, load_pipes, nearest_services, stamp_parcels,
)

LAT, LNG = -36.8663, 174.7414
_MY = 110574.0
_MX = 111320.0 * math.cos(math.radians(LAT))


def _at(east_m: float, north_m: float) -> tuple[float, float]:
    """Metres east/north of the origin, as (lat, lng)."""
    return (LAT + north_m / _MY, LNG + east_m / _MX)


def pipe(points_m: list[tuple[float, float]], **props) -> dict:
    """A pipe through the given metre offsets, as GeoJSON lon,lat."""
    coords = [[lng, lat] for lat, lng in (_at(e, n) for e, n in points_m)]
    return {"type": "Feature",
            "properties": {"id": props.pop("id", "pipe-1"), **props},
            "geometry": {"type": "LineString", "coordinates": coords}}


def parcel_feature(pid: str, area: float, at_m: tuple[float, float]) -> dict:
    h = math.sqrt(area) / 2.0
    lat0, lng0 = _at(*at_m)
    dlat, dlng = h / _MY, h / _MX
    return {"type": "Feature",
            "properties": {"id": pid, "calc_area": area,
                           "suburb_locality": "Grey Lynn",
                           "estate_description": "Fee Simple",
                           "full_address": f"{pid} Example Road"},
            "geometry": {"type": "Polygon", "coordinates": [[
                [lng0 - dlng, lat0 - dlat], [lng0 + dlng, lat0 - dlat],
                [lng0 + dlng, lat0 + dlat], [lng0 - dlng, lat0 + dlat],
                [lng0 - dlng, lat0 - dlat]]]}}


# ---- loading -----------------------------------------------------------------
def test_a_network_loads_with_what_the_operator_published(db_session):
    rep = load_pipes(db_session, [pipe([(0, 0), (100, 0)], diameter=225,
                                       material="PVC", owner="Watercare")],
                     kind="wastewater")
    db_session.commit()
    assert rep.written == 1
    got = db_session.query(ServicePipe).one()
    assert got.kind == "wastewater"
    assert got.diameter_mm == 225
    assert got.material == "PVC"


def test_the_diameter_is_kept_because_it_decides_the_answer(db_session):
    """A 100 mm rider main and a 600 mm trunk are both "a pipe" and only one of
    them takes a subdivision."""
    load_pipes(db_session, [pipe([(0, 0), (80, 0)], diameter=600)],
               kind="wastewater")
    db_session.commit()
    nets = build_networks(db_session)
    got = nearest_services(nets, *_at(0, 20))
    assert got["wastewater"].diameter_mm == 600


def test_reloading_a_network_replaces_it(db_session):
    """An operator's export is the whole network as at a date, not an
    increment. Appending one over another doubles every pipe and halves every
    distance, and nothing would catch it."""
    load_pipes(db_session, [pipe([(0, 0), (100, 0)])], kind="water")
    load_pipes(db_session, [pipe([(0, 0), (100, 0)])], kind="water")
    db_session.commit()
    assert db_session.query(ServicePipe).count() == 1


def test_the_networks_do_not_overwrite_each_other(db_session):
    load_pipes(db_session, [pipe([(0, 0), (100, 0)])], kind="water")
    load_pipes(db_session, [pipe([(0, 20), (100, 20)])], kind="wastewater")
    load_pipes(db_session, [pipe([(0, 40), (100, 40)])], kind="stormwater")
    db_session.commit()
    assert db_session.query(ServicePipe).count() == 3


def test_an_unknown_network_name_is_refused(db_session):
    with pytest.raises(ValueError):
        load_pipes(db_session, [pipe([(0, 0), (10, 0)])], kind="gas")


def test_an_nztm_network_is_refused_rather_than_loaded_into_the_sea(db_session):
    """Nothing raises on a projected coordinate. The network lands in the
    Tasman, the load reports success, and every distance in the region is then
    computed against nothing."""
    bad = {"type": "Feature", "properties": {"id": "p1"},
           "geometry": {"type": "LineString", "coordinates": [
               [1757000.0, 5921000.0], [1757100.0, 5921000.0]]}}
    with pytest.raises(ValueError) as e:
        load_pipes(db_session, [bad], kind="wastewater")
    assert "NZTM2000" in str(e.value) and "4326" in str(e.value)


# ---- the distance ------------------------------------------------------------
def test_it_measures_to_the_pipe_not_to_its_ends(db_session):
    """THE ONE THAT MATTERS for a street main. A pipe running the length of a
    street is nowhere near either of its own ends, and measuring to vertices
    reports a house opposite the middle of it as a hundred metres away."""
    load_pipes(db_session, [pipe([(0, 0), (200, 0)])], kind="wastewater")
    db_session.commit()
    nets = build_networks(db_session)
    # Standing 30 m from the middle of the pipe, 100 m from either end.
    got = nearest_services(nets, *_at(100, 30))
    assert got["wastewater"].distance_m == pytest.approx(30.0, abs=1.5)


def test_a_main_beyond_the_search_radius_is_not_reported(db_session):
    """A precise "700 m" invites being read as a cost rather than as a no."""
    load_pipes(db_session, [pipe([(0, 0), (50, 0)])], kind="wastewater")
    db_session.commit()
    nets = build_networks(db_session)
    assert nearest_services(nets, *_at(25, MAX_SEARCH_M + 200)) == {}


def test_a_long_straight_main_is_found_from_the_middle_of_it(db_session):
    """A pipe with two distant vertices would be invisible from the cells
    between them — which is exactly where the houses along it are."""
    load_pipes(db_session, [pipe([(0, 0), (2000, 0)])], kind="water")
    db_session.commit()
    nets = build_networks(db_session)
    got = nearest_services(nets, *_at(1000, 25))
    assert "water" in got, "the middle of a long main could not be found"
    assert got["water"].distance_m == pytest.approx(25.0, abs=2.0)


def test_each_network_is_measured_separately(db_session):
    load_pipes(db_session, [pipe([(0, 0), (200, 0)])], kind="wastewater")
    load_pipes(db_session, [pipe([(0, 120), (200, 120)])], kind="stormwater")
    db_session.commit()
    got = nearest_services(build_networks(db_session), *_at(100, 20))
    assert got["wastewater"].distance_m < got["stormwater"].distance_m


# ---- onto the sections -------------------------------------------------------
def test_a_parcel_is_stamped_with_its_distances(db_session):
    load_parcels(db_session, [parcel_feature("p1", 900.0, (100, 40))])
    load_pipes(db_session, [pipe([(0, 0), (200, 0)])], kind="wastewater")
    db_session.commit()
    stamp_parcels(db_session)
    db_session.commit()
    got = db_session.query(LandParcel).one()
    assert got.wastewater_m == pytest.approx(40.0, abs=3.0)


def test_a_parcel_with_nothing_nearby_stays_null_rather_than_maxed(db_session):
    """"Not known" and "nothing within 500 m" are different answers, and a
    developer would act differently on each."""
    load_parcels(db_session, [parcel_feature("far", 900.0, (0, 4000))])
    load_pipes(db_session, [pipe([(0, 0), (200, 0)])], kind="wastewater")
    db_session.commit()
    stamp_parcels(db_session)
    db_session.commit()
    assert db_session.query(LandParcel).one().wastewater_m is None


def test_no_network_loaded_leaves_every_parcel_null(db_session):
    load_parcels(db_session, [parcel_feature("p1", 900.0, (0, 0))])
    db_session.commit()
    assert stamp_parcels(db_session) == {"stormwater": 0, "wastewater": 0,
                                         "water": 0}
    assert db_session.query(LandParcel).one().wastewater_m is None


# ---- and into the search -----------------------------------------------------
def _search(db, **kw):
    from app.routers.offmarket import search
    kw.setdefault("region", "Auckland")
    kw.setdefault("suburb", None)
    kw.setdefault("min_area_m2", 0)
    kw.setdefault("zone", None)
    kw.setdefault("max_wastewater_m", None)
    kw.setdefault("include_listed", False)
    kw.setdefault("include_special_character", False)
    kw.setdefault("limit", 100)
    return search(db=db, user=None, **kw)


THAB = "Residential - Terrace Housing and Apartment Building Zone"


@pytest.fixture()
def serviced(db_session):
    load_parcels(db_session, [
        parcel_feature("near", 1400.0, (100, 40)),      # beside the main
        parcel_feature("far", 1500.0, (100, 4000)),     # nowhere near it
    ])
    load_pipes(db_session, [pipe([(0, 0), (200, 0)], diameter=225)],
               kind="wastewater")
    for p in db_session.query(LandParcel).all():
        p.zone = THAB
    db_session.commit()
    stamp_parcels(db_session)
    db_session.commit()
    return db_session


def test_the_search_reports_the_distance(serviced):
    got = _search(serviced, suburb="Grey Lynn", min_area_m2=1000, zone=THAB)
    by = {s.parcel_id: s for s in got.sites}
    assert by["near"].wastewater_m == pytest.approx(40.0, abs=3.0)
    assert by["far"].wastewater_m is None


def test_filtering_on_it_keeps_the_close_one(serviced):
    got = _search(serviced, suburb="Grey Lynn", min_area_m2=1000, zone=THAB,
                  max_wastewater_m=100)
    assert "near" in {s.parcel_id for s in got.sites}


def test_the_filter_does_not_silently_drop_what_it_cannot_judge(serviced):
    """A parcel with no figure is kept. Excluding the unknowns would empty most
    of the region the first time anybody used the filter, and look exactly like
    the region having no sites."""
    got = _search(serviced, suburb="Grey Lynn", min_area_m2=1000, zone=THAB,
                  max_wastewater_m=50)
    assert "far" in {s.parcel_id for s in got.sites}, (
        "a parcel we cannot judge was dropped as though it had been judged")


def test_a_distance_is_never_presented_as_a_right_to_connect():
    """Capacity, depth, fall and the operator's approval decide a connection,
    and none of them are in any published layer. The field names have to say
    distance and nothing else."""
    from app.routers.offmarket import Site

    fields = set(Site.model_fields)
    assert {"stormwater_m", "wastewater_m", "water_m"} <= fields
    for banned in ("can_connect", "serviced", "connectable", "has_wastewater"):
        assert banned not in fields, f"{banned} claims more than a distance"


# ---- the council's own export ------------------------------------------------
#
# Field names taken from a real Auckland Council stormwater extract, not
# guessed. The sample arrived with 49 columns and no geometry at all — it was an
# attribute-table export, which drops the shape — so what it settled is the
# NAMES, and those are worth pinning: a spelling that stops matching is a column
# that silently arrives empty, which is the failure this whole module guards.

COUNCIL = {
    "GIS ID": "{C61B3255-97E3-4714-BC1E-2ECB73BDAA57}",
    "Asset Owner": "STORMWATER",
    "Asset Diameter (mm)": 1800,
    "Pipe Material": "CONC",
    "Asset Status": "INSR",
    "Pipe Depth Downstream (m)": 2.1,
    "Length - GIS (m)": 3.90163134,
    "Stormwater Catchment": "054",
}


def _council(points_m, **over):
    props = {**COUNCIL, **over}
    coords = [[lng, lat] for lat, lng in (_at(e, n) for e, n in points_m)]
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString", "coordinates": coords}}


def test_the_councils_own_column_names_are_read(db_session):
    rep = load_pipes(db_session, [_council([(0, 0), (100, 0)])],
                     kind="stormwater")
    db_session.commit()
    got = db_session.query(ServicePipe).one()
    assert got.diameter_mm == 1800
    assert got.material == "CONC"
    assert got.owner == "STORMWATER"
    assert got.status == "INSR"
    assert got.depth_m == 2.1
    assert rep.filled.get("diameter_mm") == 1, rep.lines()


def test_a_road_drain_is_not_a_connection(db_session):
    """The export carries STORMWATER, TRANSPORT and PARKS side by side. Only
    the first is the public network. A road drain runs past sites it does not
    serve, and counting it puts a main across the road from almost everything.
    """
    rep = load_pipes(db_session, [
        _council([(0, 0), (100, 0)], **{"Asset Owner": "STORMWATER"}),
        _council([(0, 10), (100, 10)], **{"Asset Owner": "TRANSPORT"}),
        _council([(0, 20), (100, 20)], **{"Asset Owner": "PARKS"}),
    ], kind="stormwater")
    db_session.commit()
    assert rep.written == 1
    assert rep.skipped_not_public == 2
    assert db_session.query(ServicePipe).one().owner == "STORMWATER"


def test_a_decommissioned_pipe_is_not_a_connection(db_session):
    rep = load_pipes(db_session, [
        _council([(0, 0), (100, 0)]),
        _council([(0, 10), (100, 10)], **{"Asset Status": "DECM"}),
    ], kind="stormwater")
    db_session.commit()
    assert rep.written == 1 and rep.skipped_retired == 1


def test_what_was_kept_out_is_reported_not_just_dropped(db_session):
    """A load saying 22 read and 3 written is a question. A load saying 3 is a
    mystery, and the mystery surfaces weeks later as an empty map."""
    rep = load_pipes(db_session, [
        _council([(0, 0), (100, 0)]),
        _council([(0, 10), (100, 10)], **{"Asset Owner": "TRANSPORT"}),
    ], kind="stormwater")
    joined = " ".join(rep.lines())
    assert "not the public" in joined


# ---- geometry from a spreadsheet, which is how it will actually arrive -------
def test_a_wkt_column_is_read(db_session):
    """The council's extract is a spreadsheet, and the way to get geometry into
    one is a WKT column. WKT is x y — longitude first — so the flip happens
    here too, once, at the boundary."""
    from app.services_network import rows_to_features

    a, b = _at(0, 0), _at(150, 0)
    row = {**COUNCIL,
           "SHAPE": f"LINESTRING ({a[1]} {a[0]}, {b[1]} {b[0]})"}
    feats = rows_to_features([row])
    load_pipes(db_session, feats, kind="stormwater")
    db_session.commit()
    assert db_session.query(ServicePipe).count() == 1

    got = nearest_services(build_networks(db_session), *_at(75, 30))
    assert got["stormwater"].distance_m == pytest.approx(30.0, abs=2.0)


def test_the_wkt_column_is_found_whatever_it_is_called(db_session):
    """It is the one column the operator adds by hand, so it will be called
    whatever they called it."""
    from app.services_network import rows_to_features

    a, b = _at(0, 0), _at(150, 0)
    wkt = f"LINESTRING ({a[1]} {a[0]}, {b[1]} {b[0]})"
    for name in ("SHAPE", "geometry", "WKT", "the shape"):
        feats = rows_to_features([{**COUNCIL, name: wkt}])
        assert feats[0]["geometry"].get("type"), f"{name} was not found"


def test_a_spreadsheet_with_no_geometry_yields_nothing_to_place(db_session):
    """THE SAMPLE THAT ARRIVED. 49 columns, every attribute a developer could
    want, and no coordinates anywhere — an attribute-table export, which drops
    the shape. It must come back as nothing placed rather than as rows loaded
    somewhere wrong."""
    from app.services_network import rows_to_features

    rep = load_pipes(db_session, rows_to_features([dict(COUNCIL)]),
                     kind="stormwater")
    db_session.commit()
    assert rep.seen == 1
    assert rep.written == 0
    assert rep.skipped_no_geometry == 1
    assert db_session.query(ServicePipe).count() == 0


# ---- straight off the council's own service ---------------------------------
#
# "https://services1.arcgis.com/.../Stormwater_Connection/FeatureServer/0/query
#  ?outFields=*&where=1=1&f=geojson"
#
# Better than a file: a server that fetches its own data can refresh it. What it
# brings with it is one failure mode that looks exactly like success.

def _geojson_page(n: int, start: int = 0, exceeded: bool = False) -> str:
    import json as _j

    feats = []
    for i in range(n):
        a, b = _at(0, (start + i) * 30), _at(120, (start + i) * 30)
        feats.append({"type": "Feature",
                      "properties": {"OBJECTID": start + i, **COUNCIL},
                      "geometry": {"type": "LineString",
                                   "coordinates": [[a[1], a[0]], [b[1], b[0]]]}})
    body = {"type": "FeatureCollection", "features": feats}
    if exceeded:
        body["exceededTransferLimit"] = True
    return _j.dumps(body)


def test_a_layer_bigger_than_one_page_is_fetched_whole():
    """THE ONE THAT TRUNCATES A NETWORK SILENTLY. A FeatureServer will not
    return more than its maxRecordCount and does not fail when there is more —
    it answers with a page and a quiet flag. A naive fetch comes back with the
    first couple of thousand pipes looking exactly like a complete network, and
    every distance afterwards is measured against a fraction of the city."""
    from app.arcgis import fetch_layer

    calls: list[str] = []

    def get(url: str) -> str:
        calls.append(url)
        if "f=json" in url and "query" not in url:
            # A FeatureServer, which says so. Offset paging is an OPTIONAL
            # capability and a layer that does not advertise it gets paged a
            # different way — see the MapServer tests.
            return ('{"objectIdField": "OBJECTID",'
                    ' "supportedQueryFormats": "JSON, geoJSON",'
                    ' "advancedQueryCapabilities": {"supportsPagination": true}}')
        import urllib.parse
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if q.get("returnCountOnly") == "true":
            return '{"count": 7}'
        off = int(q.get("resultOffset", 0))
        return (_geojson_page(3, off, exceeded=True) if off < 6
                else _geojson_page(1, off))

    got = fetch_layer("https://x/FeatureServer/0/query", get=get, page=3)
    assert got.complete is True
    assert len(got.features) == 7, f"{len(got.features)} features over {got.pages} pages"


def test_the_pages_are_ordered_so_the_offsets_mean_something():
    """Offset paging over an unordered result is not paging: the server may
    return rows in a different order between requests, and the pages then
    overlap and skip."""
    from app.arcgis import fetch_layer

    seen: list[str] = []

    def get(url: str) -> str:
        seen.append(url)
        if "query" not in url:
            return ('{"objectIdField": "GLOBALID",'
                    ' "supportedQueryFormats": "JSON, geoJSON",'
                    ' "advancedQueryCapabilities": {"supportsPagination": true}}')
        return _geojson_page(0)

    fetch_layer("https://x/FeatureServer/0/query", get=get)
    page_calls = [u for u in seen if "resultOffset" in u]
    assert page_calls, "no page was requested"
    assert "orderByFields=GLOBALID" in page_calls[0], page_calls[0]


def test_a_projected_esri_answer_is_refused_with_the_fix_named():
    """Dropping f=geojson does not fail. The server answers in Esri's own JSON
    in the SERVICE's projection, which for a NZ council is NZTM2000 — the
    whole-network-in-the-Tasman fault arriving through a door the loader's
    guard is not watching."""
    from app.arcgis import fetch_layer

    def get(url: str) -> str:
        if "query" not in url:
            return '{"objectIdField": "OBJECTID"}'
        return ('{"spatialReference": {"wkid": 2193},'
                ' "features": [{"attributes": {"OBJECTID": 1},'
                ' "geometry": {"paths": [[[1757000, 5921000], [1757100, 5921000]]]}}]}')

    got = fetch_layer("https://x/FeatureServer/0/query", get=get)
    assert got.features == []
    assert got.complete is False
    assert "f=geojson" in got.note


def test_a_server_error_is_reported_rather_than_read_as_an_empty_layer():
    """An empty network and a refused query look identical downstream, and one
    of them means every site in the region reports no stormwater."""
    from app.arcgis import fetch_layer

    def get(url: str) -> str:
        if "query" not in url:
            return '{"objectIdField": "OBJECTID"}'
        return '{"error": {"code": 400, "message": "Invalid where clause"}}'

    got = fetch_layer("https://x/FeatureServer/0/query", get=get)
    assert got.complete is False and "Invalid where clause" in got.note


# ---- a connection layer is points, not pipes ---------------------------------
def test_a_connection_point_is_measured_to(db_session):
    """The layer is called Stormwater_Connection, and a connection point is a
    better thing to measure to than the main behind it: it is the place you
    would actually tie in."""
    lat, lng = _at(100, 0)
    load_pipes(db_session, [{
        "type": "Feature", "properties": dict(COUNCIL),
        "geometry": {"type": "Point", "coordinates": [lng, lat]}}],
        kind="stormwater")
    db_session.commit()
    got = nearest_services(build_networks(db_session), *_at(100, 45))
    assert "stormwater" in got, "a point geometry produced nothing to measure to"
    assert got["stormwater"].distance_m == pytest.approx(45.0, abs=2.0)
