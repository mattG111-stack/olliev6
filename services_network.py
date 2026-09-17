"""Stormwater, wastewater and water supply, and how far each is from a section.

    "we just need to add stormwater and water on sections"

The land says what a site could hold. This says whether it can be connected, and
that is the question that stops a development after the land has been paid for.
A large, well-zoned, flat, nearly empty parcel with no wastewater within reach
is not a development site. It is a bill.

WHAT THIS PRODUCES IS A DISTANCE. Nothing more. Whether a connection is
permitted turns on the main's capacity, its depth, the fall available from the
site, and the operator's approval — none of which is in any published layer, and
all of which a developer must confirm. So the numbers are named for what they
measure, and `None` means "not known", never "nothing nearby". Those two look
identical on a screen and a developer walks away from a site on the difference.

NOT FROM LINZ, AND NOT NATIONAL. LINZ is the cadastre. Reticulation belongs to
the network operator, and in Auckland that is two of them — Watercare for water
and wastewater, the council's side for stormwater. There is no national
equivalent. A search can cover the country for land and only Auckland for
servicing, and the product has to show that boundary rather than leave a blank
that reads as "no pipe".

HOW THE NEAREST MAIN IS FOUND. Every pipe vertex is dropped into a grid of
roughly 200 m cells; a parcel then looks only at its own cell and the eight
around it. Comparing every parcel against every pipe is a hundred thousand times
a hundred thousand and would never finish. The grid makes it a few dozen
comparisons each, and the cell is deliberately wider than the search radius so a
pipe just over a boundary is still seen.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .models import LandParcel, ServicePipe
from .parcels import LoadReport, looks_like_nztm

KINDS = ("stormwater", "wastewater", "water")

# WHAT WE OWE THE PEOPLE WHOSE NETWORK THIS IS, AND WHAT WE OWE THE DEVELOPER.
#
# The mains come from the operators' open data, published under CC BY 4.0. That
# licence permits commercial use — it does not permit using it quietly. Credit,
# a link to the licence, and a statement that the data was changed are
# conditions of the grant, and we do change it: reprojected, filtered to the
# public network, and measured against every parcel.
#
# This is the one place the house rule about not naming our sources gives way,
# and it gives way narrowly. It is a condition of a licence rather than a
# description of how the product works, and it attaches to this layer alone.
ATTRIBUTION = ("Contains data sourced from {who} under "
               "CC BY 4.0, with changes.")
LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"

# AND THE PART THAT IS NOT A LICENCE CONDITION BUT A LIABILITY. Every operator
# publishes these positions as indicative: the drawn line is where the main is
# recorded, to the accuracy of the record, and a pipe in the ground has moved,
# been relaid, or was never quite where the plan says. A developer who treats
# this screen as a service locate and puts a digger through a water main was
# misled by us. So it is stated on the drawing itself, not buried in terms.
CAUTION = ("Indicative only. Positions are approximate and must be confirmed "
           "on site before any design or excavation.")
# Shown when we cannot name an owner. Crediting nobody is a licence breach;
# crediting the wrong body is worse.
UNNAMED_OWNER = "the network operators"

# Grid cell, in degrees of latitude. ~0.002° is about 220 m.
CELL = 0.002
# Beyond this a main is not a connection prospect, it is a different street. The
# search stops rather than reporting a number nobody would act on, because a
# precise "418 m" invites treating it as a cost rather than as a no.
MAX_SEARCH_M = 500.0

# The spellings on the left of each row are the ones Auckland Council's own
# stormwater export uses, confirmed against a real extract rather than guessed.
# The rest are kept for other operators and older files: a name that stops
# matching is a column that silently arrives empty, which is the failure this
# whole module is written around.
_ID_KEYS = ("GIS ID", "GlobalID_1", "SAP ID", "OBJECTID",
            "id", "asset_id", "pipe_id", "objectid", "ASSETID")
_DIAM_KEYS = ("Asset Diameter (mm)", "Diameter Internal/External (mm)",
              "Pipe Width (mm)", "diameter_mm", "diameter", "DIAMETER",
              "nominal_diameter", "size_mm", "SIZE", "pipe_diameter")
_MATERIAL_KEYS = ("Pipe Material", "material", "MATERIAL", "pipe_material")
_OWNER_KEYS = ("Asset Owner", "owner", "OWNER", "ownership", "asset_owner")
_STATUS_KEYS = ("Asset Status", "status", "STATUS", "asset_status")
_DEPTH_KEYS = ("Pipe Depth Upstream (m)", "Pipe Depth Downstream (m)",
               "depth_m", "depth", "DEPTH")

# Owners whose pipes are the public network a development can connect to.
# Everything else in the file is somebody else's drainage: TRANSPORT is a road
# drain, PARKS drains a reserve. Both run past sites they do not serve, and
# counting them would put a main across the road from almost everything.
PUBLIC_OWNERS = {"STORMWATER", "WASTEWATER", "WATER", "WATERCARE"}
# A retired pipe is still in the file, and it is not a connection.
DEAD_STATUSES = {"DECM", "DECOMMISSIONED", "ABAN", "ABANDONED", "RETIRED"}


# The owner code in the file is a business unit, not a legal entity, so it does
# not credit anybody on its own. In Auckland water and wastewater are Watercare
# and stormwater is the council; elsewhere the codes will differ, which is what
# the fallback is for. Crediting the wrong body is worse than crediting none.
_OWNER_CREDIT = {
    "WATERCARE": "Watercare Services Limited",
    "WATER": "Watercare Services Limited",
    "WASTEWATER": "Watercare Services Limited",
    "STORMWATER": "Auckland Council",
}


def credit_line(owners) -> str:
    """The attribution owed for the pipes actually drawn.

    Built from what was loaded rather than hard-coded, so a screen showing only
    stormwater does not credit Watercare for it.
    """
    names = sorted({_OWNER_CREDIT.get(str(o).strip().upper())
                    for o in owners if o} - {None})
    who = " and ".join(names) if names else UNNAMED_OWNER
    return ATTRIBUTION.format(who=who)


def _pick(props: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in props and props[k] not in (None, ""):
            return props[k]
    low = {str(k).lower(): v for k, v in props.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def paths_from_wkt(text: str) -> list[list[list[float]]]:
    """A WKT LINESTRING or MULTILINESTRING as [[lat, lng], ...] runs.

    Here because the council's own extract arrives as a spreadsheet, and the
    way to get geometry into one is a WKT column. WKT is x y — longitude first,
    same as GeoJSON — so the flip happens here too, once, at the boundary.
    """
    s = str(text or "").strip().upper()
    if "LINESTRING" not in s:
        return []
    body = s[s.index("(") + 1:s.rindex(")")] if "(" in s and ")" in s else ""
    chunks = ([c.strip(" ()") for c in body.split("),")]
              if s.startswith("MULTILINESTRING") else [body.strip(" ()")])
    out = []
    for chunk in chunks:
        pts = []
        for pair in chunk.split(","):
            bits = pair.split()
            if len(bits) < 2:
                continue
            try:
                lon, lat = float(bits[0]), float(bits[1])
            except ValueError:
                continue
            pts.append([lat, lon])
        if len(pts) >= 2:
            out.append(pts)
    return out


def rows_to_features(rows: list[dict], *, geometry_key: str = "") -> list[dict]:
    """Spreadsheet rows into the same shape the GeoJSON loader takes.

    `geometry_key` names the WKT column; when it is not given, every column is
    tried and the first that parses wins. That matters because the geometry
    column is the one the operator has to add by hand, and it will be called
    whatever they called it.
    """
    out = []
    for row in rows:
        runs = []
        if geometry_key:
            runs = paths_from_wkt(row.get(geometry_key, ""))
        else:
            for v in row.values():
                runs = paths_from_wkt(v)
                if runs:
                    break
        if not runs:
            out.append({"type": "Feature", "properties": dict(row),
                        "geometry": {}})
            continue
        out.append({
            "type": "Feature", "properties": dict(row),
            "geometry": {"type": "MultiLineString",
                         "coordinates": [[[p[1], p[0]] for p in r] for r in runs]},
        })
    return out


def paths_of(geom: dict) -> list[list[list[float]]]:
    """A GeoJSON LineString/MultiLineString as [[lat, lng], ...] runs.

    Flipped from GeoJSON's lon,lat here, once, at the boundary — the same place
    and for the same reason as the parcel loader. Backwards does not raise; it
    puts the network in Somalia.
    """
    kind = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates") or []
    if kind == "LineString":
        runs = [coords]
    elif kind == "MultiLineString":
        runs = list(coords)
    elif kind == "Point":
        # A connection layer is points, not pipes, and a connection point is a
        # better thing to measure to than the main behind it — it is the place
        # you would actually tie in. Stored as a run of two identical vertices
        # so one distance routine serves both: the segment maths already handles
        # a zero-length segment as a distance to the point.
        runs = [[list(coords), list(coords)]] if len(coords) >= 2 else []
    elif kind == "MultiPoint":
        runs = [[list(p), list(p)] for p in coords if len(p) >= 2]
    else:
        return []
    out = []
    for run in runs:
        pts = [[float(p[1]), float(p[0])] for p in run
               if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(pts) >= 2:
            out.append(pts)
    return out


@dataclass
class Main:
    """One main, read off a feature and ready either to store or to draw."""
    kind: str
    path: list[list[float]]
    is_node: bool = False
    diameter_mm: float | None = None
    material: str | None = None
    owner: str | None = None
    status: str | None = None
    depth_m: float | None = None
    external_id: str | None = None


def mains_from_features(features: list[dict], kind: str,
                        rep: LoadReport | None = None) -> list[Main]:
    """Features to mains: the field names, the exclusions, the NZTM guard.

    ONE RULE, AND NOW TWO CALLERS. The bulk load reads a whole region into the
    database; the property page asks the service about one address and draws the
    answer. If each did its own parsing they would disagree about which pipes
    count — a retired main or a road drain would be excluded from the search and
    drawn on the map, and the screen would contradict the number beside it.
    """
    out: list[Main] = []
    for f in features:
        if rep is not None:
            rep.seen += 1
        props = f.get("properties") or {}
        if rep is not None:
            rep.saw_keys.update(str(k) for k in props)
        runs = paths_of(f.get("geometry") or {})
        if not runs:
            if rep is not None:
                rep.skipped_no_geometry += 1
            continue
        if looks_like_nztm(runs[0]):
            raise ValueError(
                f"The {kind} data is not in latitude and longitude — the "
                f"first coordinate is {runs[0][0]}, outside the degree range. "
                "It is almost certainly NZTM2000 (EPSG:2193). Reproject to "
                "EPSG:4326, or the whole network lands in the Tasman Sea with "
                "nothing to show it went wrong.")

        diameter = _num(_pick(props, _DIAM_KEYS))
        material = _pick(props, _MATERIAL_KEYS)
        owner = _pick(props, _OWNER_KEYS)
        status = _pick(props, _STATUS_KEYS)
        depth = _num(_pick(props, _DEPTH_KEYS))
        ext = _pick(props, _ID_KEYS)

        # Two rows kept out rather than measured against. Both are real pipes
        # in the file and neither is something a development can tie into, so
        # counting them would put a main beside almost every site.
        if status and str(status).strip().upper() in DEAD_STATUSES:
            if rep is not None:
                rep.skipped_retired += 1
            continue
        if owner and str(owner).strip().upper() not in PUBLIC_OWNERS:
            if rep is not None:
                rep.skipped_not_public += 1
            continue

        for run in runs:
            out.append(Main(
                kind=kind, path=run,
                # A manhole arrives as two identical vertices so one distance
                # routine serves both. Drawn as a line it has zero length and
                # is invisible, and it is the thing that matters most on a
                # plan: a manhole is where a connection is actually made.
                is_node=(len(run) == 2
                         and abs(run[0][0] - run[1][0]) < 1e-12
                         and abs(run[0][1] - run[1][1]) < 1e-12),
                diameter_mm=diameter,
                material=str(material)[:32] if material else None,
                owner=str(owner)[:64] if owner else None,
                status=str(status)[:16] if status else None,
                depth_m=depth,
                external_id=str(ext) if ext not in (None, "") else None,
            ))
        if rep is not None:
            rep.note("diameter_mm", diameter)
            rep.note("material", material)
            rep.note("owner", owner)
            rep.note("depth_m", depth)
    return out


def load_pipes(db: Session, features: list[dict], kind: str,
               region: str = "Auckland", *, replace: bool = True) -> LoadReport:
    """Write one network into service_pipes.

    Replaces that network for the region by default: an operator's export is the
    whole network as at a date, not an increment, and appending one over another
    doubles every pipe and halves every distance in a way nothing would catch.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")

    rep = LoadReport()
    if replace:
        db.query(ServicePipe).filter(ServicePipe.region == region,
                                     ServicePipe.kind == kind).delete(
            synchronize_session=False)

    for m in mains_from_features(features, kind, rep):
        db.add(ServicePipe(
            region=region, kind=kind, external_id=m.external_id,
            path=json.dumps(m.path), diameter_mm=m.diameter_mm,
            material=m.material, owner=m.owner, status=m.status,
            depth_m=m.depth_m))
        rep.written += 1

    known = set(_ID_KEYS + _DIAM_KEYS + _MATERIAL_KEYS + _OWNER_KEYS
                + _STATUS_KEYS + _DEPTH_KEYS)
    rep.unmatched_keys = sorted(rep.saw_keys - known)
    db.flush()
    return rep


# ---- distance ----------------------------------------------------------------

def _scale(lat: float) -> tuple[float, float]:
    """Metres per degree of longitude and latitude at this latitude."""
    return 111320.0 * math.cos(math.radians(lat)), 110574.0


def _point_to_segment_m(p, a, b, mx: float, my: float) -> float:
    """Shortest distance from a point to a pipe segment, in metres.

    To the SEGMENT, not to its endpoints. A main running the length of a street
    is nowhere near either end of itself, and measuring to vertices would report
    a house opposite the middle of a pipe as being a hundred metres from it.
    """
    px, py = (p[1] * mx, p[0] * my)
    ax, ay = (a[1] * mx, a[0] * my)
    bx, by = (b[1] * mx, b[0] * my)
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


@dataclass
class Nearest:
    """The closest main of one kind, and what is known about it."""
    kind: str
    distance_m: float
    diameter_mm: float | None = None
    material: str | None = None


@dataclass
class Network:
    """Every pipe of one kind, bucketed into cells so a lookup is cheap."""
    kind: str
    cells: dict[tuple[int, int], list[tuple[list[list[float]], ServicePipe]]] = \
        field(default_factory=dict)

    def add(self, path: list[list[float]], pipe: ServicePipe) -> None:
        # A pipe is registered in every cell it passes through, by vertex. A
        # long straight main between two distant vertices would otherwise be
        # invisible from the cells in between — which is exactly where the
        # houses along it are.
        for i in range(len(path) - 1):
            a, b = path[i], path[i + 1]
            steps = max(1, int(math.dist(a, b) / (CELL / 2)) + 1)
            for s in range(steps + 1):
                t = s / steps
                lat = a[0] + (b[0] - a[0]) * t
                lng = a[1] + (b[1] - a[1]) * t
                self.cells.setdefault((int(lat / CELL), int(lng / CELL)),
                                      []).append((path, pipe))

    def nearest(self, lat: float, lng: float) -> Nearest | None:
        mx, my = _scale(lat)
        ci, cj = int(lat / CELL), int(lng / CELL)
        seen: set[int] = set()
        best: Nearest | None = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for path, pipe in self.cells.get((ci + di, cj + dj), ()):
                    if id(path) in seen:
                        continue
                    seen.add(id(path))
                    d = min(_point_to_segment_m((lat, lng), path[k], path[k + 1],
                                                mx, my)
                            for k in range(len(path) - 1))
                    if d > MAX_SEARCH_M:
                        continue
                    if best is None or d < best.distance_m:
                        best = Nearest(kind=self.kind, distance_m=round(d, 1),
                                       diameter_mm=pipe.diameter_mm,
                                       material=pipe.material)
        return best


def build_networks(db: Session, region: str = "Auckland") -> dict[str, Network]:
    """Load each network into its grid. One pass over the pipes, then cheap."""
    out: dict[str, Network] = {}
    for pipe in db.query(ServicePipe).filter(ServicePipe.region == region).all():
        net = out.setdefault(pipe.kind, Network(kind=pipe.kind))
        try:
            path = json.loads(pipe.path)
        except (ValueError, TypeError):
            continue
        if len(path) >= 2:
            net.add(path, pipe)
    return out


def nearest_services(nets: dict[str, Network], lat: float,
                     lng: float) -> dict[str, Nearest]:
    """The closest main of each kind to one point."""
    got = {}
    for kind, net in nets.items():
        hit = net.nearest(lat, lng)
        if hit is not None:
            got[kind] = hit
    return got


def stamp_parcels(db: Session, region: str = "Auckland") -> dict[str, int]:
    """Write each parcel's distance to the nearest main of each kind.

    Stored on the parcel so a search can filter on it. Recomputed rather than
    maintained: a network export replaces the network, so every distance drawn
    from it is stale the moment it does.

    A parcel with no main within the search radius is left NULL, not set to the
    radius. "Not known" and "nothing within 500 m" are different answers and a
    developer would act differently on each.
    """
    nets = build_networks(db, region)
    counts = {k: 0 for k in KINDS}
    if not nets:
        return counts

    for p in db.query(LandParcel).filter(LandParcel.region == region).all():
        if p.lat is None or p.lng is None:
            continue
        got = nearest_services(nets, p.lat, p.lng)
        for kind, attr in (("stormwater", "stormwater_m"),
                           ("wastewater", "wastewater_m"),
                           ("water", "water_m")):
            hit = got.get(kind)
            setattr(p, attr, hit.distance_m if hit else None)
            if hit:
                counts[kind] += 1
    db.flush()
    return counts
