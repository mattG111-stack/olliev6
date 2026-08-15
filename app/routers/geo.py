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

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..security import require_active
from ..models import BuildingOverride, ParcelCache, User

router = APIRouter(prefix="/api/geo", tags=["geo"])

# NZ Primary Parcels on the LINZ Data Service. The geometry column is `shape`.
LINZ_LAYER = "layer-50772"
LINZ_WFS = "https://data.linz.govt.nz/services;key={key}/wfs"


class Parcel(BaseModel):
    """A section boundary. `ring` is [[lat, lng], ...], closed, outer ring only.

    `source` tells the client what it is looking at, so the panel can say so
    rather than implying a surveyed boundary it doesn't have:
      linz     — the legal parcel.
      none     — no boundary available; the client falls back to a box sized
                 from the listing's land area.
    """
    source: str
    ring: list[list[float]] = []
    area_m2: float | None = None
    appellation: str | None = None


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
    row = (
        db.query(ParcelCache)
        .filter(ParcelCache.lat_key == lat_k, ParcelCache.lng_key == lng_k)
        .first()
    )
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

    db.add(ParcelCache(
        lat_key=lat_k,
        lng_key=lng_k,
        status=found.source,
        ring=json.dumps(found.ring) if found.ring else None,
        area_m2=found.area_m2,
        appellation=found.appellation,
    ))
    db.commit()
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
    rows = (
        db.query(BuildingOverride)
        .filter(BuildingOverride.property_id == property_id)
        .order_by(BuildingOverride.idx)
        .all()
    )
    return Buildings(buildings=[
        Building(
            is_subject=bool(r.is_subject), east_m=r.east_m, north_m=r.north_m,
            width_m=r.width_m, depth_m=r.depth_m, rot_deg=r.rot_deg,
            height_m=r.height_m, label=r.label,
        ) for r in rows
    ])


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

    db.query(BuildingOverride).filter(BuildingOverride.property_id == property_id).delete()
    for i, b in enumerate(payload.buildings):
        db.add(BuildingOverride(
            property_id=property_id, idx=i, is_subject=b.is_subject,
            east_m=b.east_m, north_m=b.north_m, width_m=b.width_m, depth_m=b.depth_m,
            rot_deg=b.rot_deg % 360, height_m=b.height_m, label=b.label,
            updated_by=user.id,
        ))
    db.commit()
    return payload
