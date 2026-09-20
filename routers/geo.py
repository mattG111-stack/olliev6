"""Legal parcel boundaries, for the Sun & shade panel.

The sun map needs the real shape of the section, not a box: shade only means
something once you can see how much of *this* property it covers, and a rectangle
on the pin disagrees visibly with the fences in the aerial photo.

Boundaries come from Toitū Te Whenua LINZ — the NZ Primary Parcels layer, which is
the authoritative record and free to use under CC BY. The request is made here
rather than in the browser for two reasons: the LINZ key is a secret and would be
readable in a client bundle, and LINZ does not promise CORS headers to arbitrary
origins.

Every lookup is cached, misses included. A property outside LINZ coverage should
cost one request ever, not one per page view.
"""
from __future__ import annotations

import json
import math
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from security import require_active
from models import (BuildingOverride, ParcelCache, PropertyForSale,
                      ServicePipe, User)
from site_layout import LOT_DEPTH_M, LOT_WIDTH_M, terrace_layout

router = APIRouter(prefix="/api/geo", tags=["geo"])

# NZ Primary Parcels on the LINZ Data Service. The geometry column is `shape`.
LINZ_LAYER = "layer-50772"
LINZ_WFS = "https://data.linz.govt.nz/services;key={key}/wfs"

# THE OPERATORS' OWN SERVICES, ASKED PER PROPERTY. Set as environment variables
# so a layer can be pointed at, moved or switched off without a deploy — these
# are other people's endpoints and they are renumbered and retired on their
# timetable, not ours.
#
#   SERVICE_URL_WATER       = ".../FeatureServer/<n>/query"
#   SERVICE_URL_WASTEWATER  = ".../FeatureServer/<n>/query"
#   SERVICE_URL_STORMWATER  = ".../FeatureServer/<n>/query"
#
# Prefer the operator's OPEN DATA service over the one behind their public map
# viewer: the open data is published under CC BY 4.0, which is the licence we
# rely on, and the viewer's own backend carries different terms and can be
# closed off without notice.
# MORE THAN ONE LAYER PER KIND, separated by commas. The mains and the nodes
# are published as DIFFERENT LAYERS — a wastewater network is a "Wastewater
# Main" layer and a "Wastewater Manhole" layer, and a viewer that draws both is
# drawing two layers. Allowing only one URL per kind would have left the
# manholes off the map for ever, which is the half of the drawing that matters
# most: a manhole is where a connection actually gets made.
SERVICE_URLS = {
    kind: [u.strip() for u
           in (os.environ.get(f"SERVICE_URL_{kind.upper()}") or "").split(",")
           if u.strip()]
    for kind in ("water", "wastewater", "stormwater")
}


class Edge(BaseModel):
    """One side of the section: how long it is, and which way it runs."""
    length_m: float
    bearing_deg: float          # 0 = north, rising clockwise


class Parcel(BaseModel):
    """A section boundary. `ring` is [[lat, lng], ...], closed, outer ring only.

    `source` tells the client what it is looking at, so the panel can say so
    rather than implying a surveyed boundary it doesn't have:
      linz     — the legal parcel.
      none     — no boundary available; the client falls back to a box sized
                 from the listing's land area.

    THE DIMENSIONS ARE PART OF THE ANSWER, NOT JUST PART OF THE PICTURE.
    The map has drawn a length against each side for a while, and that is where
    the numbers stopped: on a canvas, in one panel, unavailable to a search, an
    export or anything asking about a site nobody has listed. "A developer can
    look for houses that are zoned right and the dimensions" is a filter, and a
    filter needs the numbers as numbers.

    Derived here from the ring rather than stored alongside it, so a dimension
    can never disagree with the boundary it was measured off — and so the cached
    parcels already on file gain the measurements without being re-fetched.
    """
    source: str
    ring: list[list[float]] = []
    area_m2: float | None = None
    appellation: str | None = None
    # Every side, in ring order, so the shape can be read as well as measured.
    edges: list[Edge] = []
    perimeter_m: float | None = None
    # The longest boundary. Not called "frontage": which side faces the road is
    # a question about roads, and there is no road data here to answer it with.
    # Naming it frontage would be a guess wearing a measurement's clothes.
    longest_side_m: float | None = None

    @model_validator(mode="after")
    def _measure(self):
        if not self.edges and len(self.ring) >= 3:
            self.edges = _ring_edges(self.ring)
        if self.edges:
            self.perimeter_m = round(sum(e.length_m for e in self.edges), 1)
            self.longest_side_m = max(e.length_m for e in self.edges)
        return self


def _key(v: float) -> str:
    """Round to 5 dp (~1 m). Two lookups on the same house share a cache row."""
    return f"{v:.5f}"


def _ring_area_m2(ring: list[list[float]]) -> float:
    """Shoelace area of a small lat/lng ring, via a local equirectangular
    projection. Good to well under a percent at section scale, and it avoids
    pulling in a geodesy dependency for one number."""
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[0] for p in ring) / len(ring)
    mx = 111320.0 * math.cos(math.radians(lat0))
    my = 110574.0
    pts = [(p[1] * mx, p[0] * my) for p in ring]
    total = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _ring_edges(ring: list[list[float]]) -> list[Edge]:
    """Each side of a parcel, in metres and degrees.

    Same local flat projection the area uses, for the same reason: at the scale
    of one section it is accurate to well under a percent, and it avoids a
    geodesy dependency for a number that is going to be printed to one decimal
    place.

    A closed ring repeats its first point at the end. Left in, that produces a
    final side of zero metres, which is not a boundary — it would show up as a
    "0.0 m" label on the map and as a nonsense minimum in any filter.

    Sides shorter than half a metre are left out. They are survey artefacts:
    the tiny chamfer where two boundaries meet, or a kink around a service
    easement. They are real in the title and meaningless as dimensions, and
    including them makes every corner look like an extra side of the section.
    """
    pts = [p for p in ring]
    if len(pts) >= 2 and abs(pts[0][0] - pts[-1][0]) < 1e-9 \
            and abs(pts[0][1] - pts[-1][1]) < 1e-9:
        pts.pop()
    if len(pts) < 3:
        return []

    lat0 = sum(p[0] for p in pts) / len(pts)
    mx = 111320.0 * math.cos(math.radians(lat0))
    my = 110574.0

    out: list[Edge] = []
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        east = (b[1] - a[1]) * mx
        north = (b[0] - a[0]) * my
        length = math.hypot(east, north)
        if length < 0.5:
            continue
        out.append(Edge(
            length_m=round(length, 1),
            bearing_deg=round(math.degrees(math.atan2(east, north)) % 360.0, 1),
        ))
    return out


def _outer_ring(geom: dict) -> list[list[float]]:
    """Pull the outer ring out of a GeoJSON Polygon/MultiPolygon as [lat, lng].

    LINZ serves EPSG:4326 as lon,lat in GeoJSON — which is correct for the format
    and the opposite of what Leaflet wants, hence the flip here rather than
    somewhere further downstream where it would be easy to miss.
    """
    kind = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates") or []
    if kind == "Polygon" and coords:
        outer = coords[0]
    elif kind == "MultiPolygon" and coords:
        # Largest part wins — a parcel split by a road comes back as several.
        outer = max((p[0] for p in coords if p), key=len, default=[])
    else:
        return []
    return [[float(lat), float(lon)] for lon, lat in outer]


async def _fetch_linz(lat: float, lng: float) -> Parcel:
    """Ask LINZ which parcel contains this point."""
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": LINZ_LAYER,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": "1",
        # CQL takes POINT in x y order, i.e. lon lat.
        "cql_filter": f"Contains(shape,POINT({lng} {lat}))",
    }
    url = LINZ_WFS.format(key=settings.linz_api_key)
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    features = data.get("features") or []
    if not features:
        return Parcel(source="none")

    props = features[0].get("properties") or {}
    ring = _outer_ring(features[0].get("geometry") or {})
    if len(ring) < 4:
        return Parcel(source="none")

    # Prefer LINZ's own survey area; fall back to the ring's own geometry.
    area = props.get("calc_area") or props.get("survey_area")
    return Parcel(
        source="linz",
        ring=ring,
        area_m2=float(area) if area else _ring_area_m2(ring),
        appellation=props.get("appellation"),
    )


@router.get("/parcel", response_model=Parcel)
async def parcel(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    db: Session = Depends(get_db),
) -> Parcel:
    """The legal boundary containing this point, or `source: "none"`.

    Never raises on a LINZ problem. A boundary is a nice-to-have on a panel whose
    real job is the sun, so an outage degrades to the fallback box rather than
    taking the property page down with it.
    """
    lat_k, lng_k = _key(lat), _key(lng)
    # The cache lookup is wrapped because the table may not exist yet: a deploy
    # that ships this router before db_bootstrap has created parcel_cache would
    # otherwise 500 the property page over a boundary it can live without.
    try:
        row = (
            db.query(ParcelCache)
            .filter(ParcelCache.lat_key == lat_k, ParcelCache.lng_key == lng_k)
            .first()
        )
    except Exception:
        db.rollback()
        return Parcel(source="none")
    if row is not None:
        return Parcel(
            source=row.status,
            ring=json.loads(row.ring) if row.ring else [],
            area_m2=row.area_m2,
            appellation=row.appellation,
        )

    if not settings.linz_api_key:
        return Parcel(source="none")

    try:
        found = await _fetch_linz(lat, lng)
    except Exception:
        # Don't cache a transient failure as a permanent miss.
        return Parcel(source="none")

    try:
        db.add(ParcelCache(
            lat_key=lat_k,
            lng_key=lng_k,
            status=found.source,
            ring=json.dumps(found.ring) if found.ring else None,
            area_m2=found.area_m2,
            appellation=found.appellation,
        ))
        db.commit()
    except Exception:
        # Caching is an optimisation. Failing to store the answer must not
        # discard the answer.
        db.rollback()
    return found


# ---------------------------------------------------------------------------
# Buildings that cast shade onto a property
# ---------------------------------------------------------------------------

class Building(BaseModel):
    """One building, as metres east/north of the listing's coordinates."""
    is_subject: bool = False
    east_m: float = 0.0
    north_m: float = 0.0
    width_m: float
    depth_m: float
    rot_deg: float = 0.0
    height_m: float = 3.5
    label: str | None = None


class Buildings(BaseModel):
    buildings: list[Building] = []


@router.get("/buildings/{property_id}", response_model=Buildings)
def get_buildings(property_id: int, db: Session = Depends(get_db)) -> Buildings:
    """Hand-placed buildings for a property, subject and neighbours alike.

    Empty means nobody has traced this property yet, and the panel falls back to
    a footprint derived from floor area — which casts the subject's own shadow
    and nothing else.
    """
    # Same guard as the parcel lookup: no saved buildings is the normal state for
    # almost every property, and a missing table must read the same as "none
    # saved" rather than breaking the panel.
    try:
        rows = (
            db.query(BuildingOverride)
            .filter(BuildingOverride.property_id == property_id)
            .order_by(BuildingOverride.idx)
            .all()
        )
    except Exception:
        db.rollback()
        return Buildings(buildings=[])
    return Buildings(buildings=[
        Building(
            is_subject=bool(r.is_subject), east_m=r.east_m, north_m=r.north_m,
            width_m=r.width_m, depth_m=r.depth_m, rot_deg=r.rot_deg,
            height_m=r.height_m, label=r.label,
        ) for r in rows
    ])


# The widest view we will ask an operator's service about. A map zooms out to
# the whole country, and "draw me every pipe in New Zealand" is a request that
# times out for us and hurts them. Past this the panel keeps a window around the
# property rather than refusing: a developer who zooms out wants context, not an
# error, and the pipes near the site are the context.
MAX_VIEW_M = 2_500.0


def _view_box(north, south, east, west, lat: float, lng: float,
              radius_m: float) -> tuple[float, float, float, float] | None:
    """The map's bounds as a query envelope, or None when there are none.

    All four or nothing: three sides of a rectangle is not a rectangle, and
    guessing the fourth would quietly query somewhere else.
    """
    # isinstance rather than "is not None". An omitted FastAPI query parameter
    # arrives here as the Query object itself when this is called directly, and
    # a Query is not None — it sails through the check and blows up on the
    # first comparison. Caught once already this build, in the driver.
    sides = [v for v in (north, south, east, west)
             if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(sides) != 4:
        return None
    north, south, east, west = sides
    if north <= south or east <= west:
        # A view that crosses the antimeridian or arrives inverted. Neither is
        # a New Zealand property page, and a negative-width envelope matches
        # nothing — an empty answer that reads as a site with no services.
        return None

    half_lat = min((north - south) / 2, MAX_VIEW_M / 110_574.0)
    half_lng = min((east - west) / 2,
                   MAX_VIEW_M / (111_320.0 * max(math.cos(math.radians(lat)),
                                                 1e-6)))
    mid_lat = (north + south) / 2
    mid_lng = (east + west) / 2
    return (mid_lng - half_lng, mid_lat - half_lat,
            mid_lng + half_lng, mid_lat + half_lat)


def _crosses(path: list, lat: float, lng: float,
             box_lat: float, box_lng: float) -> bool:
    """Does any part of this pipe fall inside the window?

    Segment by segment, comparing bounding boxes. Conservative on purpose: a
    segment whose box overlaps but which misses the window diagonally is kept,
    and keeping a pipe that is nearly in view costs one extra line on a map.
    Dropping one that is in view costs the map its point.

    A node arrives as two identical vertices, which reduces this to asking
    whether the point is in the window — the right question for it.
    """
    north, south = lat + box_lat, lat - box_lat
    east, west = lng + box_lng, lng - box_lng
    for a, b in zip(path, path[1:]):
        if max(a[0], b[0]) < south or min(a[0], b[0]) > north:
            continue
        if max(a[1], b[1]) < west or min(a[1], b[1]) > east:
            continue
        return True
    return False


def _stored_mains(db: Session, region: str) -> list:
    """Whatever has been loaded into the database for this region."""
    from services_network import Main

    out = []
    for p in db.query(ServicePipe).filter(ServicePipe.region == region).all():
        try:
            path = json.loads(p.path)
        except (ValueError, TypeError):
            continue
        out.append(Main(
            kind=p.kind, path=path,
            is_node=(len(path) == 2
                     and abs(path[0][0] - path[1][0]) < 1e-12
                     and abs(path[0][1] - path[1][1]) < 1e-12),
            diameter_mm=p.diameter_mm, material=p.material, owner=p.owner,
            status=p.status, depth_m=p.depth_m, external_id=p.external_id))
    return out


def _live_mains(lat: float, lng: float, radius_m: float,
                box: tuple[float, float, float, float] | None = None) -> list:
    """Ask each operator's service what runs near this point.

    Per kind, because water, wastewater and stormwater are three layers and
    often three different publishers. A kind with no URL configured is simply
    absent — the panel then shows the ones we do have rather than nothing.

    NEVER RAISES. An operator's service being slow or down is not a reason for
    a property page to fail; it is a reason for that layer to be missing from
    it, and the rest of the page is still worth showing.
    """
    from services_network import KINDS, mains_from_features

    out = []
    capped = False
    for kind in KINDS:
        for url in SERVICE_URLS.get(kind) or []:
            try:
                from arcgis import fetch_envelope, fetch_near

                got = (fetch_envelope(url, *box) if box
                       else fetch_near(url, lat, lng, radius_m))
                # A SERVICE THAT CAPPED ITS ANSWER HANDED BACK A FRACTION OF THE
                # NETWORK AND SAID SO QUIETLY. Drawn without a word, that is a
                # map with streets missing their mains, and nothing on it to
                # distinguish "no pipe here" from "we did not ask for enough".
                # It is the same silent truncation guarded in the bulk fetch,
                # arriving through the live door.
                if not got.complete and got.features:
                    capped = True
                if got.features:
                    out.extend(mains_from_features(got.features, kind))
            except Exception:                          # noqa: BLE001
                # One layer failing is one layer missing, not a failed page —
                # and not a reason to skip the other layers of the same kind.
                continue
    return out, capped


class ServiceLine(BaseModel):
    """One main, ready to draw: its path and what is known about it."""
    kind: str                       # stormwater | wastewater | water
    path: list[list[float]] = []    # [[lat, lng], ...]
    # A MANHOLE IS NOT A SHORT PIPE. The operator publishes nodes as their own
    # layer and every plan draws them as a circle, because a manhole is where a
    # connection is actually made — the pipe between two of them is just the
    # pipe. Stored as a run of two identical vertices so one distance routine
    # serves both, which means the renderer cannot tell them apart without
    # being told, and a node drawn as a line is a line of zero length: invisible.
    is_node: bool = False
    diameter_mm: float | None = None
    material: str | None = None
    # "AC50", "C1100" — the label the operator's own plans use, material and
    # bore together. Built here rather than in the browser so the map and any
    # export say the same thing about the same pipe.
    label: str | None = None


class Services(BaseModel):
    """The underground services around a property."""
    lines: list[ServiceLine] = []
    # Metres to the nearest of each kind, for the summary line above the map.
    nearest: dict[str, float] = {}
    note: str = ""
    # A licence condition, not a courtesy: the open data these mains come from
    # is granted on the condition that the operator is credited, the licence is
    # linked, and changes are declared. Sent with the lines rather than kept in
    # a terms page, because it has to appear wherever the lines appear.
    attribution: str = ""
    licence_url: str = ""
    # The operator's service capped its answer, so this is SOME of the network
    # rather than all of it in view. Said out loud, because a map with streets
    # missing their mains and nothing to explain it is worse than a smaller map
    # — a developer reads a gap as "no pipe here".
    partial: bool = False
    # THE ONE THAT MATTERS ON A BUILDING SITE. These positions are indicative.
    # A developer who reads this screen as a service locate and puts a digger
    # through a water main was misled by us, so it is stated on the drawing.
    caution: str = ""


@router.get("/services", response_model=Services)
def services(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_m: float = Query(250, ge=50, le=1000),
    # THE MAP'S OWN BOUNDS, when it has them. A developer pans and zooms, and
    # what they expect to see is the network across the streets in front of
    # them — a circle round the pin ends mid-road and looks like missing data.
    # Distances are still measured from the pin: the view decides what is drawn,
    # never what "the nearest main" means.
    north: float | None = Query(None, ge=-90, le=90),
    south: float | None = Query(None, ge=-90, le=90),
    east: float | None = Query(None, ge=-180, le=180),
    west: float | None = Query(None, ge=-180, le=180),
    region: str = "Auckland",
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> Services:
    """Water, wastewater and stormwater mains near a point.

    Drawn on the property map the way the operator's own plans draw them,
    because that is the drawing a developer already knows how to read. What it
    shows is where the mains RUN — it is not a service locate, and digging
    without one is how pipes get hit.
    """
    from services_network import (CAUTION, LICENCE_URL, _point_to_segment_m,
                                    _scale, credit_line)

    box = _view_box(north, south, east, west, lat, lng, radius_m)
    if box is not None:
        west_, south_, east_, north_ = box
        box_lat = max(abs(north_ - lat), abs(lat - south_))
        box_lng = max(abs(east_ - lng), abs(lng - west_))
    else:
        box_lat = radius_m / 110574.0
        box_lng = radius_m / (111320.0 * math.cos(math.radians(lat)))
    mx, my = _scale(lat)

    out = Services()
    mains = _stored_mains(db, region)
    if not mains:
        # NOTHING LOADED IS NOT THE SAME AS NOTHING TO SHOW. The operators run
        # a service that answers "what is near this point" in one request, and
        # a property page has a point. Asking it beats telling the customer to
        # come back after somebody runs an import — and the answer is current
        # rather than as current as the last one.
        mains, capped = _live_mains(lat, lng, radius_m, box)
        if not mains:
            out.note = ("Underground services are not available for this "
                        "property yet.")
            return out
        if capped:
            out.partial = True

    best: dict[str, float] = {}
    for p in mains:
        path = p.path
        if len(path) < 2:
            continue
        # Cheap reject before any distance maths. A region's network is a
        # hundred thousand pipes and a property page must not walk all of them
        # in earnest.
        #
        # PER SEGMENT, NOT PER VERTEX. A main is one long polyline down a
        # street and its vertices sit wherever the surveyor put them — often
        # hundreds of metres away, at the corners. Asking whether a VERTEX is
        # in view drops every pipe that merely PASSES THROUGH it, which is all
        # the ones that matter: the main outside the house disappears the
        # moment you zoom in on the house. Same mistake as measuring to the
        # ends of a pipe, in the other half of the same panel.
        if not _crosses(path, lat, lng, box_lat, box_lng):
            continue

        # TO THE PIPE, NOT TO ITS ENDS. A main runs the length of a street and
        # is nowhere near either end of itself, so measuring to vertices reports
        # a house opposite the middle of a main as being a hundred metres from
        # it. The search module has always measured to the segment; this panel
        # measured to the vertices, and the two printed different numbers about
        # the same pipe — 107 m against 29 m on a main across the road.
        near = min(_point_to_segment_m([lat, lng], a, b, mx, my)
                   for a, b in zip(path, path[1:]))
        if p.kind not in best or near < best[p.kind]:
            best[p.kind] = near

        bits = [str(p.material).strip() if p.material else "",
                f"{int(p.diameter_mm)}" if p.diameter_mm else ""]
        out.lines.append(ServiceLine(
            kind=p.kind, path=path, is_node=p.is_node,
            diameter_mm=p.diameter_mm, material=p.material,
            # A node has no bore to print, and labelling one with a diameter
            # would describe the pipe it sits on rather than the manhole.
            label=None if p.is_node else ("".join(bits) or None),
        ))

    out.nearest = {k: round(v, 1) for k, v in best.items()}
    if not out.lines:
        out.note = f"No mains within {int(radius_m)} m of this property."
        return out
    # Credited from the pipes actually drawn, so a screen showing only
    # stormwater does not credit the water operator for it.
    out.attribution = credit_line({p.owner for p in mains})
    out.licence_url = LICENCE_URL
    out.caution = CAUTION
    return out


class TerraceLayout(BaseModel):
    """A starting arrangement of terraces on the real boundary.

    `fits` is how many of the yield's terraces could actually be placed inside
    the parcel; `by_area` is what the area arithmetic claimed. When they differ
    the shape is the binding constraint and the drawing is the evidence.

    `buildings` is deliberately the same shape the editor already saves, so a
    developer drags these exactly like the ones they place by hand — this gives
    them a starting point to argue with, not a layout to accept.
    """
    fits: int
    by_area: int
    shape_limited: bool = False
    buildings: list[Building] = []
    note: str = ""


@router.get("/terrace-layout/{property_id}", response_model=TerraceLayout)
async def terrace_layout_for(
    property_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> TerraceLayout:
    """Where the terraces this site was costed for could actually go.

    NOT A SITE PLAN, and the note it returns says so. Three things it does not
    know: which side is the road, the zone's coverage, setback, outdoor-living
    and height-to-boundary controls, and the ground. It answers one question —
    does the shape take the number we quoted — and it answers it usefully in
    one direction. Fewer than the arithmetic claimed is a finding, because the
    rectangles genuinely do not fit. As many is only the absence of one
    objection.
    """
    p = db.query(PropertyForSale).filter(PropertyForSale.id == property_id).first()
    if p is None:
        raise HTTPException(status_code=404, detail="Not found")
    want = int(p.dwellings or 0)
    if not want or p.latitude is None or p.longitude is None:
        return TerraceLayout(fits=0, by_area=want,
                             note="No terrace yield for this site.")

    got = await parcel(lat=float(p.latitude), lng=float(p.longitude), db=db)
    if got.source != "linz" or len(got.ring) < 4:
        # The estimated square is not a boundary, and packing houses onto it
        # would be a drawing of the square root of the land area. Same rule as
        # the dimension labels.
        return TerraceLayout(
            fits=0, by_area=want,
            note="No surveyed boundary for this site, so there is nothing to "
                 "lay out against.")

    laid = terrace_layout(got.ring, want,
                          origin=(float(p.latitude), float(p.longitude)))
    return TerraceLayout(
        fits=laid.fits,
        by_area=laid.by_area,
        shape_limited=laid.shape_limited,
        buildings=[
            Building(
                is_subject=False,
                east_m=sum(x for x, _ in lot.corners) / 4.0,
                north_m=sum(y for _, y in lot.corners) / 4.0,
                width_m=LOT_WIDTH_M, depth_m=LOT_DEPTH_M,
                # Rotation from the lot's own first side, so a row laid against
                # an angled boundary arrives on that angle rather than square to
                # the map.
                rot_deg=math.degrees(math.atan2(
                    lot.corners[1][0] - lot.corners[0][0],
                    lot.corners[1][1] - lot.corners[0][1])) % 360.0,
                height_m=6.5,             # two storeys, which is what casts the shade
                label=f"Terrace {i + 1}",
            ) for i, lot in enumerate(laid.lots)
        ],
        note=("Starting arrangement only. It does not know which side is the "
              "road, the zone's setbacks or coverage, or the ground — move "
              "them."),
    )


@router.put("/buildings/{property_id}", response_model=Buildings)
def put_buildings(
    property_id: int,
    payload: Buildings,
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> Buildings:
    """Replace the whole set for a property.

    Replace rather than patch: the editor always holds the complete picture, and
    a partial update would let a half-finished drag leave an orphaned building
    casting shade nobody can see the source of.
    """
    if len(payload.buildings) > 40:
        raise HTTPException(status_code=422, detail="too many buildings (max 40)")
    for b in payload.buildings:
        # A zero or negative footprint casts a degenerate shadow; a 200 m building
        # is a fat-fingered drag, not a neighbour.
        if not (1.0 <= b.width_m <= 120 and 1.0 <= b.depth_m <= 120):
            raise HTTPException(status_code=422, detail="building footprint out of range")
        if not (0.5 <= b.height_m <= 60):
            raise HTTPException(status_code=422, detail="building height out of range")
        if abs(b.east_m) > 300 or abs(b.north_m) > 300:
            raise HTTPException(status_code=422, detail="building too far from the property")

    # A WRITE, by contrast, must not pretend to succeed. If the table is missing
    # the user needs to know their edit was not saved, not watch it vanish on the
    # next reload.
    try:
        db.query(BuildingOverride).filter(BuildingOverride.property_id == property_id).delete()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503,
                            detail="Building storage is not ready yet — try again shortly")
    for i, b in enumerate(payload.buildings):
        db.add(BuildingOverride(
            property_id=property_id, idx=i, is_subject=b.is_subject,
            east_m=b.east_m, north_m=b.north_m, width_m=b.width_m, depth_m=b.depth_m,
            rot_deg=b.rot_deg % 360, height_m=b.height_m, label=b.label,
            updated_by=user.id,
        ))
    db.commit()
    return payload
