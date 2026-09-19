"""Sections worth approaching, whether or not anybody is selling them.

    "if you're a Developer and you're in Grey Lynn and you miss out on a
     property you're looking for a over 1000 m2 zoned Terrace houses can we
     find them?"

That is two stored attributes and a join. It was unanswerable only because the
rows did not exist: boundaries were fetched one pin at a time, and a cache keyed
on the pins we happened to look at only ever holds the houses that WERE for
sale — the exact opposite of the question.

The tests here are mostly about the ways a search like this misleads, which are
worse than it failing:

    An empty screen. A suburb with no zoning loaded and a suburb with no
    matching sites look identical, and they need opposite responses.

    A list of sites that cannot be touched. In the inner-west a section can be
    zoned for terraces and carry a villa that cannot legally come down, and a
    developer would know that before the end of the first page.

    A parcel assessed on a guessed zone. The zone is the engine's first gate —
    a Single House section is never subdividable whatever its size — so filling
    a blank turns a house into a development site on no evidence at all.

    And the quiet one: a file in the wrong projection. NZ exports are routinely
    NZTM2000, a projected coordinate is just a bigger number, nothing raises,
    and the whole suburb lands in the Tasman Sea looking like it loaded fine.
"""
from __future__ import annotations

import json
import math

import pytest

from models import LandParcel
from parcels import apply_zones, load_parcels, looks_like_nztm, ring_area_m2

THAB = "Residential - Terrace Housing and Apartment Building Zone"
SINGLE = "Residential - Single House Zone"

LAT, LNG = -36.8663, 174.7414          # Grey Lynn
_MY = 110574.0
_MX = 111320.0 * math.cos(math.radians(LAT))


def sq(area_m2: float, *, at: tuple[float, float] = (LAT, LNG)) -> list[list[float]]:
    """A square parcel of a given area, as GeoJSON lon,lat."""
    h = math.sqrt(area_m2) / 2.0
    lat0, lng0 = at
    dlat, dlng = h / _MY, h / _MX
    return [[[lng0 - dlng, lat0 - dlat], [lng0 + dlng, lat0 - dlat],
             [lng0 + dlng, lat0 + dlat], [lng0 - dlng, lat0 + dlat],
             [lng0 - dlng, lat0 - dlat]]]


def feature(pid: str, area: float, *, suburb="Grey Lynn", title="Fee Simple",
            address=None, at=(LAT, LNG), zone=None) -> dict:
    props = {"id": pid, "calc_area": area, "suburb_locality": suburb,
             "estate_description": title,
             "full_address": address or f"{pid} Example Road"}
    if zone:
        props["zone"] = zone
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Polygon", "coordinates": sq(area, at=at)}}


def _load(db, feats, region="Auckland"):
    rep = load_parcels(db, feats, region=region)
    db.commit()
    return rep


# ---- the rows exist at all ---------------------------------------------------
def test_a_section_nobody_listed_is_still_loaded(db_session):
    rep = _load(db_session, [feature("p1", 1200.0)])
    assert rep.written == 1
    got = db_session.query(LandParcel).one()
    assert got.area_m2 == pytest.approx(1200.0)
    assert got.suburb == "Grey Lynn"
    assert got.title_type == "Fee Simple"


def test_reloading_the_extract_updates_rather_than_duplicates(db_session):
    _load(db_session, [feature("p1", 1200.0)])
    _load(db_session, [feature("p1", 1250.0)])
    rows = db_session.query(LandParcel).all()
    assert len(rows) == 1, "a refreshed extract became a second copy of the suburb"
    assert rows[0].area_m2 == pytest.approx(1250.0)


def test_the_report_says_what_the_file_did_not_carry(db_session):
    """A load that silently filled nothing looks exactly like a load that
    worked, until somebody searches and gets an empty screen days later."""
    bare = {"type": "Feature",
            "properties": {"id": "p9", "calc_area": 1400.0},
            "geometry": {"type": "Polygon", "coordinates": sq(1400.0)}}
    rep = _load(db_session, [bare])
    assert rep.filled.get("zone", 0) == 0
    assert any("zone: NOTHING FOUND" in line for line in rep.lines())


def test_the_area_is_computed_when_the_file_has_none(db_session):
    bare = {"type": "Feature", "properties": {"id": "p8"},
            "geometry": {"type": "Polygon", "coordinates": sq(900.0)}}
    _load(db_session, [bare])
    assert db_session.query(LandParcel).one().area_m2 == pytest.approx(900.0, rel=0.02)


# ---- the projection trap -----------------------------------------------------
def test_an_nztm_export_is_refused_rather_than_loaded_into_the_sea():
    """THE QUIET ONE. A projected coordinate is just a bigger number. Nothing
    raises, nothing warns, and the whole suburb lands in the Tasman looking
    like it loaded fine."""
    nztm = [[1757000.0, 5921000.0], [1757040.0, 5921000.0],
            [1757040.0, 5921030.0], [1757000.0, 5921030.0]]
    assert looks_like_nztm([[y, x] for x, y in nztm]) is True
    assert looks_like_nztm([[LAT, LNG]]) is False


def test_the_refusal_names_the_projection_and_the_fix(db_session):
    bad = {"type": "Feature", "properties": {"id": "p1", "calc_area": 1200.0},
           "geometry": {"type": "Polygon", "coordinates": [[
               [1757000.0, 5921000.0], [1757040.0, 5921000.0],
               [1757040.0, 5921030.0], [1757000.0, 5921030.0],
               [1757000.0, 5921000.0]]]}}
    with pytest.raises(ValueError) as e:
        load_parcels(db_session, [bad])
    assert "NZTM2000" in str(e.value) and "4326" in str(e.value)


def test_lon_lat_is_read_as_lat_lng_and_not_the_other_way(db_session):
    """GeoJSON is lon,lat and everything downstream is lat,lng. Backwards does
    not raise; it puts Auckland in Somalia."""
    _load(db_session, [feature("p1", 1000.0)])
    got = db_session.query(LandParcel).one()
    assert -47 < got.lat < -34, f"latitude came back as {got.lat}"
    assert 166 < got.lng < 179, f"longitude came back as {got.lng}"


# ---- the zone, which decides everything --------------------------------------
def test_a_parcel_takes_the_zone_its_centre_falls_in(db_session):
    _load(db_session, [feature("p1", 1200.0)])
    zone_poly = {"type": "Feature", "properties": {"ZONE": THAB},
                 "geometry": {"type": "Polygon", "coordinates": sq(500_000.0)}}
    apply_zones(db_session, [zone_poly])
    db_session.commit()
    assert db_session.query(LandParcel).one().zone == THAB


def test_a_parcel_outside_every_zone_polygon_keeps_no_zone(db_session):
    """Blank stays blank. The zone is the engine's first gate, so filling it in
    turns a Single House section into a development site on no evidence."""
    _load(db_session, [feature("p1", 1200.0)])
    far = {"type": "Feature", "properties": {"ZONE": THAB},
           "geometry": {"type": "Polygon", "coordinates": sq(500.0, at=(-41.3, 174.8))}}
    apply_zones(db_session, [far])
    db_session.commit()
    assert db_session.query(LandParcel).one().zone is None


# ---- the Grey Lynn question --------------------------------------------------
@pytest.fixture()
def greylynn(db_session):
    """A suburb: two big THAB sites, one big Single House site, one small THAB
    site, and one big THAB site under special character."""
    feats = [
        feature("big-1", 1400.0, at=(LAT, LNG)),
        feature("big-2", 1100.0, at=(LAT + 0.002, LNG)),
        feature("single", 1600.0, at=(LAT + 0.004, LNG)),
        feature("small", 420.0, at=(LAT + 0.006, LNG)),
        feature("character", 1300.0, at=(LAT + 0.008, LNG)),
        feature("other-suburb", 2000.0, suburb="Papakura", at=(LAT + 0.1, LNG)),
    ]
    _load(db_session, feats)
    by = {p.linz_id: p for p in db_session.query(LandParcel).all()}
    for k in ("big-1", "big-2", "small", "character"):
        by[k].zone = THAB
    by["single"].zone = SINGLE
    by["other-suburb"].zone = THAB
    by["character"].special_character = True
    db_session.commit()
    return db_session


def _search(db, **kw):
    from routers.offmarket import search
    kw.setdefault("region", "Auckland")
    kw.setdefault("suburb", None)
    kw.setdefault("min_area_m2", 0)
    kw.setdefault("zone", None)
    kw.setdefault("include_listed", False)
    kw.setdefault("include_special_character", False)
    kw.setdefault("limit", 100)
    return search(db=db, user=None, **kw)


def test_the_question_as_asked(greylynn):
    """Over 1,000 m², zoned for terraces, in Grey Lynn."""
    got = _search(greylynn, suburb="Grey Lynn", min_area_m2=1000, zone=THAB)
    found = {s.parcel_id for s in got.sites}
    assert found == {"big-1", "big-2"}, found
    assert all(s.area_m2 >= 1000 for s in got.sites)


def test_the_suburb_next_door_is_not_in_the_answer(greylynn):
    got = _search(greylynn, suburb="Grey Lynn", min_area_m2=1000, zone=THAB)
    assert "other-suburb" not in {s.parcel_id for s in got.sites}


def test_a_single_house_section_never_appears_however_big(greylynn):
    """The engine's first rule, and the reason a guessed zone is dangerous."""
    got = _search(greylynn, suburb="Grey Lynn", min_area_m2=1000)
    assert "single" not in {s.parcel_id for s in got.sites}


def test_a_special_character_site_is_left_out_and_counted(greylynn):
    """Not silently dropped. A developer needs to know the list would be longer
    if they were willing to argue with an overlay."""
    got = _search(greylynn, suburb="Grey Lynn", min_area_m2=1000, zone=THAB)
    assert "character" not in {s.parcel_id for s in got.sites}
    assert got.special_character == 1
    with_it = _search(greylynn, suburb="Grey Lynn", min_area_m2=1000, zone=THAB,
                      include_special_character=True)
    assert "character" in {s.parcel_id for s in with_it.sites}


def test_each_site_carries_the_yield_and_the_shape_check(greylynn):
    got = _search(greylynn, suburb="Grey Lynn", min_area_m2=1000, zone=THAB)
    s = got.sites[0]
    assert s.dwellings and s.dwellings > 0
    assert s.strategy
    assert s.fits is not None, "the boundary was never tested against the yield"
    assert s.fits <= s.dwellings


def test_no_profit_is_quoted_for_a_house_nobody_has_priced(greylynn):
    """"This section takes five terraces" is a fact about the land. "This
    section makes $900,000" would need a price nobody has been quoted."""
    from routers.offmarket import Site
    assert "profit" not in Site.model_fields
    assert "gross_sales" not in Site.model_fields


# ---- the empty screen, which is the real failure mode ------------------------
def test_nothing_loaded_does_not_look_like_nothing_matching(db_session):
    got = _search(db_session, suburb="Grey Lynn", min_area_m2=1000, zone=THAB)
    assert got.sites == []
    assert "no parcels are loaded" in got.note.lower()


def test_a_suburb_with_no_zoning_says_so(db_session):
    _load(db_session, [feature(f"p{i}", 1500.0, at=(LAT + i * 0.001, LNG))
                       for i in range(6)])
    got = _search(db_session, suburb="Grey Lynn", min_area_m2=1000)
    assert got.sites == []
    assert got.no_zone == 6
    assert "zoning" in got.note.lower()


def test_too_large_a_minimum_names_the_size_rather_than_returning_nothing(greylynn):
    got = _search(greylynn, suburb="Grey Lynn", min_area_m2=50_000)
    assert got.sites == []
    assert "50,000" in got.note or "largest" in got.note.lower()


def test_a_misspelled_suburb_is_told_apart_from_an_empty_one(greylynn):
    got = _search(greylynn, suburb="Greylynne", min_area_m2=1000)
    assert got.sites == []
    assert "Greylynne" in got.note


# ---- where the middle of a section is ----------------------------------------
#
# Found by a servicing test reporting 36.3 m from a pipe the parcel was 40 m
# from. The point decides which zone a parcel takes and how far it is from a
# wastewater main, and it was wrong twice over.

def test_the_centre_is_the_centre_of_the_section(db_session):
    """Not the average corner, and not thrown off by the closing point.

    A closed ring repeats its first point, so averaging all of them counts that
    corner twice and drags the centre toward it — 3.7 m on a 1,400 m² square.
    """
    _load(db_session, [feature("p1", 1400.0)])
    got = db_session.query(LandParcel).one()
    assert got.lat == pytest.approx(LAT, abs=1e-6)
    assert got.lng == pytest.approx(LNG, abs=1e-6)


def test_the_centre_survives_a_boundary_surveyed_in_short_runs(db_session):
    """Vertices are not evenly spaced on a real title. One side surveyed in ten
    short runs against a single long straight one pulls the average corner
    toward the detailed side, and the average corner is not the centre."""
    h = math.sqrt(1600.0) / 2.0
    dlat, dlng = h / _MY, h / _MX
    # A square whose southern boundary carries ten intermediate points.
    south = [[LNG - dlng + (2 * dlng) * (i / 10.0), LAT - dlat]
             for i in range(11)]
    ring = south + [[LNG + dlng, LAT + dlat], [LNG - dlng, LAT + dlat],
                    [LNG - dlng, LAT - dlat]]
    _load(db_session, [{
        "type": "Feature",
        "properties": {"id": "detailed", "calc_area": 1600.0,
                       "suburb_locality": "Grey Lynn"},
        "geometry": {"type": "Polygon", "coordinates": [ring]}}])
    got = db_session.query(LandParcel).one()
    off_m = abs(got.lat - LAT) * _MY
    assert off_m < 1.0, f"the centre sits {off_m:.1f} m toward the detailed side"


def test_the_centre_is_not_noise_at_new_zealand_coordinates(db_session):
    """The shoelace terms are differences of products of coordinates near
    (-36.9, 174.7) while a section spans 0.0003 of a degree. Computed on the raw
    degrees the answer is mostly floating-point noise, and it does not fail
    loudly: it put the centre 13 m outside a 1,400 m² parcel."""
    _load(db_session, [feature("p1", 1400.0)])
    got = db_session.query(LandParcel).one()
    half = math.sqrt(1400.0) / 2.0
    assert abs(got.lat - LAT) * _MY < half, "the centre landed outside the parcel"
    assert abs(got.lng - LNG) * _MX < half
