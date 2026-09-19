"""Every section in a suburb, loaded from a file rather than fetched.

    "if you're a Developer and you're in Grey Lynn and you miss out on a
     property you're looking for a over 1000 m2 zoned Terrace houses can we
     find them?"

Yes, and the hard part was already built: the subdivision engine takes a zone,
a land area, a title type and a section rate, and never asks whether the house
is for sale. What was missing is the rows. The parcel boundary is fetched one
pin at a time, which is right for a property page and useless for a suburb —
and a cache keyed on pins only ever holds the houses that WERE for sale, which
is the opposite of the question.

FROM A FILE, ON PURPOSE. The parcel layer is a regional extract and the zoning
comes from a council portal; both are large, both change rarely, and neither is
reachable from the machine this runs on. Loading from an export also means the
exact bytes that produced a result can be kept and re-run, which a live query
cannot offer.

WRITTEN TO SURVIVE FIELD NAMES NOBODY HAS SEEN YET. The LINZ names are known
because the probe script already reads them — calc_area, appellation,
estate_description. The council's are not. So every field is looked up through
a list of candidate spellings and what was actually found is REPORTED, not
assumed: a load that silently filled nothing looks exactly like a load that
worked until somebody searches. Same principle as probe_linz.py, which prints
what is there rather than what the schema claims.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .models import LandParcel
from .site_layout import _inside

# Candidate spellings, most specific first. Extend rather than rename: a file
# exported last year must keep loading.
_AREA_KEYS = ("calc_area", "survey_area", "land_area", "parcel_area",
              "area_m2", "AREA", "Shape__Area")
_ID_KEYS = ("id", "parcel_intent_id", "parcel_id", "OBJECTID", "objectid")
_APPELLATION_KEYS = ("appellation", "parcel_appellation", "legal_description")
_TITLE_KEYS = ("estate_description", "type", "estate", "tenure", "title_type")
_ADDRESS_KEYS = ("full_address", "address", "full_address_ascii")
_SUBURB_KEYS = ("suburb_locality", "suburb", "locality", "town_city")
_ZONE_KEYS = ("ZONE", "zone", "ZONE_RESOLVED", "Zone_Name", "zone_name",
              "ZONE_DESC", "UP_ZONE", "zone_description")


@dataclass
class LoadReport:
    """What the file actually contained. Printed, not assumed.

    `filled` counts the fields that were found per name. A zero against a name
    is the finding: the export does not carry it under any spelling tried, and
    the search that depends on it will return nothing for a reason that has
    nothing to do with the data being wrong.
    """
    seen: int = 0
    written: int = 0
    skipped_no_geometry: int = 0
    # Networks only: pipes in the file that are not a connection prospect. Both
    # counted rather than dropped quietly, because a load reporting 22 rows read
    # and 3 written is a question, and a load reporting 3 is a mystery.
    skipped_retired: int = 0
    skipped_not_public: int = 0
    filled: dict[str, int] = field(default_factory=dict)
    unmatched_keys: list[str] = field(default_factory=list)
    # Every property name seen, so a column the loader does not read can be
    # reported rather than silently ignored — a renamed field is the failure
    # this whole report exists to catch.
    saw_keys: set[str] = field(default_factory=set)

    def note(self, name: str, value) -> None:
        if value is not None and value != "":
            self.filled[name] = self.filled.get(name, 0) + 1

    def lines(self) -> list[str]:
        out = [f"{self.seen:,} features read, {self.written:,} parcels written"]
        if self.skipped_no_geometry:
            out.append(f"{self.skipped_no_geometry:,} skipped with no usable boundary")
        if self.skipped_not_public:
            out.append(f"{self.skipped_not_public:,} skipped — not the public "
                       f"network (road or park drainage)")
        if self.skipped_retired:
            out.append(f"{self.skipped_retired:,} skipped — decommissioned")
        for name in ("area_m2", "appellation", "title_type", "address",
                     "suburb", "zone"):
            got = self.filled.get(name, 0)
            if got == 0:
                out.append(f"  {name}: NOTHING FOUND — no candidate field matched")
            else:
                out.append(f"  {name}: {got:,} of {self.written:,}")
        if self.unmatched_keys:
            out.append("  fields present in the file and not read: "
                       + ", ".join(sorted(self.unmatched_keys)[:14]))
        return out


def _pick(props: dict, keys: tuple[str, ...]):
    """First candidate spelling that carries a value."""
    for k in keys:
        if k in props and props[k] not in (None, ""):
            return props[k]
    # Case-insensitively, because exports differ on capitalisation alone and a
    # miss here reads as missing data rather than as a naming difference.
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
    return f if f == f else None          # NaN out


def outer_ring(geom: dict) -> list[list[float]]:
    """A GeoJSON Polygon/MultiPolygon as [[lat, lng], ...].

    GeoJSON is lon,lat and everything downstream here is lat,lng. The flip
    happens once, here, rather than somewhere it would be easy to miss — the
    same reason the geo router does it at its own boundary. Getting it backwards
    does not raise; it puts Auckland in Somalia.
    """
    kind = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates") or []
    if kind == "Polygon" and coords:
        outer = coords[0]
    elif kind == "MultiPolygon" and coords:
        outer = max((p[0] for p in coords if p), key=len, default=[])
    else:
        return []
    out = []
    for pair in outer:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        lon, lat = float(pair[0]), float(pair[1])
        out.append([lat, lon])
    return out


def looks_like_nztm(ring: list[list[float]]) -> bool:
    """Is this file in NZTM2000 rather than latitude and longitude?

    NZ network and council exports are routinely projected, and a projected
    coordinate is just a bigger number — nothing raises, nothing warns, and the
    whole layer lands in the Tasman Sea looking like it loaded fine. New Zealand
    latitudes run about -34 to -47 and longitudes 166 to 179; NZTM northings are
    in the millions. Anything outside the degree range is not degrees.
    """
    if not ring:
        return False
    lat, lng = ring[0][0], ring[0][1]
    return not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0)


def _centroid(ring: list[list[float]]) -> tuple[float, float]:
    """The centre of a parcel — the area's centre, not the average corner.

    TWO FAULTS IN THE OBVIOUS VERSION, and this point decides which zone a
    parcel takes and how far it is from a wastewater main.

    A closed ring repeats its first point. Averaging all of them counts that
    corner twice and drags the centre toward it: measured on a 1,400 m² square,
    3.7 m out of place, which showed up as a servicing distance of 36.3 m where
    the parcel was 40 m from the pipe.

    And the average corner is not the centre anyway. Vertices are not evenly
    spaced on a real title — a boundary surveyed in ten short runs against one
    long straight one pulls the average toward the detailed side, and on a
    concave parcel the average can land outside the section altogether, which
    for a point-in-polygon zone lookup means taking the neighbour's zoning.

    So: the area-weighted centroid, with the vertex mean kept only for the
    degenerate case where the shoelace term vanishes.
    """
    pts = [p for p in ring]
    if len(pts) >= 2 and abs(pts[0][0] - pts[-1][0]) < 1e-12 \
            and abs(pts[0][1] - pts[-1][1]) < 1e-12:
        pts.pop()
    if not pts:
        return (0.0, 0.0)
    mean = (sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts))
    if len(pts) < 3:
        return mean

    # Computed about a local origin and shifted back, NOT on the raw degrees.
    # The shoelace terms are differences of products of coordinates near
    # (-36.9, 174.7), while a section spans about 0.0003 of a degree — so in
    # absolute coordinates every term is a tiny difference of two large and
    # nearly equal numbers, and the answer is mostly floating-point noise. It
    # does not fail loudly: measured on a 1,400 m² square it put the centre
    # 13 m outside the parcel, which is a whole zone away on a corner site.
    ox, oy = pts[0][1], pts[0][0]
    a2 = 0.0
    cx = 0.0      # longitude, relative to the origin
    cy = 0.0      # latitude, relative to the origin
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        px, py = p[1] - ox, p[0] - oy
        qx, qy = q[1] - ox, q[0] - oy
        cross = px * qy - qx * py
        a2 += cross
        cx += (px + qx) * cross
        cy += (py + qy) * cross
    if abs(a2) < 1e-18:
        return mean
    return (oy + cy / (3.0 * a2), ox + cx / (3.0 * a2))


def ring_area_m2(ring: list[list[float]]) -> float:
    """Shoelace area through a local flat projection — the same one the geo
    router uses, so a parcel measured here and one measured there agree."""
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[0] for p in ring) / len(ring)
    mx = 111320.0 * math.cos(math.radians(lat0))
    pts = [(p[1] * mx, p[0] * 110574.0) for p in ring]
    total = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def load_parcels(db: Session, features: list[dict], region: str = "Auckland",
                 *, replace: bool = False) -> LoadReport:
    """Write a parcel extract into land_parcels. Returns what it found.

    `features` is a GeoJSON FeatureCollection's `features`. Reloading the same
    region updates rows on the LINZ id rather than duplicating them, so a
    refreshed extract is a re-run and not a second copy.
    """
    rep = LoadReport()
    if replace:
        db.query(LandParcel).filter(LandParcel.region == region).delete(
            synchronize_session=False)

    existing = {p.linz_id: p for p in
                db.query(LandParcel).filter(LandParcel.region == region).all()}
    read_keys = set(_AREA_KEYS + _ID_KEYS + _APPELLATION_KEYS + _TITLE_KEYS
                    + _ADDRESS_KEYS + _SUBURB_KEYS + _ZONE_KEYS)
    saw_keys: set[str] = set()

    for f in features:
        rep.seen += 1
        props = f.get("properties") or {}
        saw_keys.update(str(k) for k in props)
        ring = outer_ring(f.get("geometry") or {})
        if len(ring) < 4:
            rep.skipped_no_geometry += 1
            continue
        if looks_like_nztm(ring):
            raise ValueError(
                "This export is not in latitude and longitude — the first "
                f"coordinate is {ring[0]}, which is outside the degree range. "
                "It is almost certainly NZTM2000 (EPSG:2193). Reproject to "
                "EPSG:4326 before loading, or every parcel lands in the sea "
                "with nothing to show it went wrong.")

        pid = _pick(props, _ID_KEYS)
        if pid in (None, ""):
            rep.skipped_no_geometry += 1
            continue
        lat, lng = _centroid(ring)
        area = _num(_pick(props, _AREA_KEYS)) or ring_area_m2(ring)

        row = existing.get(str(pid))
        if row is None:
            row = LandParcel(linz_id=str(pid), region=region)
            db.add(row)
            existing[str(pid)] = row

        row.ring = json.dumps(ring)
        row.lat, row.lng = lat, lng
        row.area_m2 = area
        row.appellation = _pick(props, _APPELLATION_KEYS)
        row.title_type = _pick(props, _TITLE_KEYS)
        row.address = _pick(props, _ADDRESS_KEYS)
        row.suburb = _pick(props, _SUBURB_KEYS)
        # Only set from the parcel file when it happens to carry one. Usually it
        # does not, and apply_zones() fills it from the council layer.
        zone = _pick(props, _ZONE_KEYS)
        if zone:
            row.zone = str(zone)

        rep.written += 1
        for name, val in (("area_m2", area), ("appellation", row.appellation),
                          ("title_type", row.title_type), ("address", row.address),
                          ("suburb", row.suburb), ("zone", row.zone)):
            rep.note(name, val)

    rep.unmatched_keys = sorted(saw_keys - {k for k in read_keys})
    db.flush()
    return rep


def apply_zones(db: Session, zone_features: list[dict], region: str = "Auckland",
                *, zone_keys: tuple[str, ...] = _ZONE_KEYS) -> LoadReport:
    """Stamp each parcel with the zone polygon its centre falls inside.

    A centre point rather than the whole boundary. A parcel straddling two
    zones is real and rare, and resolving it properly means clipping polygon
    against polygon and deciding which share wins — a judgement this has no
    basis to make. The centre is one clear rule, and a parcel on a zone
    boundary is exactly the kind of site somebody should look at by hand.
    """
    rep = LoadReport()
    polys: list[tuple[str, list[tuple[float, float]]]] = []
    for f in zone_features:
        rep.seen += 1
        name = _pick(f.get("properties") or {}, zone_keys)
        ring = outer_ring(f.get("geometry") or {})
        if not name or len(ring) < 4:
            rep.skipped_no_geometry += 1
            continue
        if looks_like_nztm(ring):
            raise ValueError(
                "The zone layer is not in latitude and longitude — reproject "
                "to EPSG:4326 before loading.")
        polys.append((str(name), [(p[1], p[0]) for p in ring]))   # x=lng, y=lat

    if not polys:
        return rep

    for p in db.query(LandParcel).filter(LandParcel.region == region).all():
        if p.lat is None or p.lng is None:
            continue
        for name, poly in polys:
            if _inside((p.lng, p.lat), poly):
                p.zone = name
                rep.written += 1
                rep.note("zone", name)
                break
    db.flush()
    return rep


def load_geojson(path: str) -> list[dict]:
    """A GeoJSON file's features, whichever of the two shapes it is in."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        return data.get("features") or []
    if isinstance(data, list):
        return data
    return [data] if isinstance(data, dict) and data.get("geometry") else []
