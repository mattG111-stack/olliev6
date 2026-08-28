"""Ask the free data sources what they actually hold for one address.

Runs server-side for two reasons. The LINZ key is a secret and would be readable
in a browser bundle, and LINZ does not promise CORS headers to arbitrary origins
— the same two reasons the parcel lookup in routers/geo.py is server-side.

Every field that comes back is returned verbatim, not just the ones expected.
The purpose of this is to find out what is there, and a probe that only reports
the fields someone already thought of cannot tell you about the one that would
have saved a line on a data invoice.
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)

WFS = "https://data.linz.govt.nz/services;key={key}/wfs"
ADDRESS_LAYER = "layer-123113"
TIMEOUT = 12.0

# The free layers worth asking, and why each one is on the list. `geom` is the
# geometry column, which differs per layer and is the usual reason a spatial
# filter returns an empty result rather than an error.
LAYERS = [
    {"id": ADDRESS_LAYER, "name": "NZ Addresses", "geom": "shape", "mode": "bbox",
     "why": "resolves the address, and carries suburb / town / postcode"},
    {"id": "layer-50772", "name": "NZ Primary Parcels", "geom": "shape", "mode": "contains",
     "why": "land area (β4) and the legal description"},
    {"id": "layer-50804", "name": "NZ Property Titles", "geom": "shape", "mode": "contains",
     "why": "estate type → fee simple / cross-lease / unit title (β10–13)"},
    {"id": "layer-101292", "name": "NZ Building Outlines", "geom": "shape", "mode": "contains",
     "why": "roof footprint — a sanity check on floor area, not floor area"},
]

# What the pricing model takes, and the field names that would satisfy each.
# Matched loosely against whatever comes back, so a layer that names something
# unexpectedly still registers.
MODEL_INPUTS: list[tuple[str, str, list[str]]] = [
    ("β2",  "Capital value", ["capital_value", "rating_value", "cv"]),
    ("β3",  "Floor area",    ["floor_area", "floorarea"]),
    ("β4",  "Land area",     ["calc_area", "survey_area", "land_area", "parcel_area"]),
    ("β5",  "Bedrooms",      ["bedroom", "beds"]),
    ("β6",  "Bathrooms",     ["bathroom", "baths"]),
    ("β7",  "Car spaces",    ["carspace", "car_space", "garage"]),
    ("β8",  "Age / year built", ["year_built", "decade_built", "construction_year"]),
    ("β10", "Title type",    ["estate_description", "estate", "tenure"]),
    ("—",   "Suburb / district", ["suburb_locality", "suburb", "town_city", "territorial_auth"]),
]


class ProbeUnavailable(RuntimeError):
    """No key, so there is nothing to ask. Carries a message the admin can act on."""


def _params(layer: dict, lat: float, lng: float) -> dict:
    p = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": layer["id"], "outputFormat": "application/json",
        "srsName": "EPSG:4326", "count": "3",
    }
    if layer["mode"] == "contains":
        # CQL takes POINT as x y — lon lat. Reversed, it returns an empty result
        # rather than an error, which is the most expensive kind of mistake.
        p["cql_filter"] = f"Contains({layer['geom']},POINT({lng} {lat}))"
    else:
        d = 0.0004                       # ~40 m: one property, not the street
        p["cql_filter"] = f"BBOX({layer['geom']},{lng - d},{lat - d},{lng + d},{lat + d})"
    return p


async def geocode(client: httpx.AsyncClient, text: str) -> dict | None:
    """Address text → a point, through LINZ's own address layer.

    Deliberately not a commercial geocoder: if the free layer cannot find an
    Auckland address reliably, that is worth discovering now rather than after
    building a product on it.
    """
    safe = text.replace("'", "").strip()
    if not safe:
        return None
    r = await client.get(WFS.format(key=settings.linz_api_key), params={
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": ADDRESS_LAYER, "outputFormat": "application/json",
        "srsName": "EPSG:4326", "count": "1",
        "cql_filter": f"full_address ILIKE '%{safe}%'",
    })
    r.raise_for_status()
    feats = (r.json().get("features") or [])
    if not feats:
        return None
    coords = ((feats[0].get("geometry") or {}).get("coordinates") or [None, None])
    if coords[0] is None:
        return None
    props = feats[0].get("properties") or {}
    return {"lat": float(coords[1]), "lng": float(coords[0]),
            "matched": props.get("full_address") or text}


async def probe(*, address: str | None = None,
                lat: float | None = None, lng: float | None = None) -> dict:
    """Everything the free layers hold for one point, plus a coverage scorecard."""
    if not settings.linz_api_key:
        raise ProbeUnavailable(
            "No LINZ_API_KEY is set on this server, so there is nothing to ask. "
            "Get one free at data.linz.govt.nz (this is the Data Service key, a "
            "different key from LINZ Basemaps) and set LINZ_API_KEY."
        )

    out: dict = {"matched": None, "lat": lat, "lng": lng, "layers": [], "coverage": []}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        if lat is None or lng is None:
            if not address:
                raise ProbeUnavailable("Give an address, or a latitude and longitude.")
            found = await geocode(client, address)
            if not found:
                raise ProbeUnavailable(
                    f"LINZ has no address matching {address!r}. Try more of the "
                    f"street address, or enter a latitude and longitude instead.")
            out.update(found)
            lat, lng = found["lat"], found["lng"]

        # field name → (layer it came from, value). First layer to supply a
        # field wins, which is why the layers are ordered cheapest-first.
        seen: dict[str, tuple[str, object]] = {}
        reached = 0

        for layer in LAYERS:
            row = {"name": layer["name"], "id": layer["id"], "why": layer["why"],
                   "status": 0, "features": 0, "fields": {}, "error": None}
            try:
                r = await client.get(WFS.format(key=settings.linz_api_key),
                                     params=_params(layer, lat, lng))
                row["status"] = r.status_code
                if r.status_code in (401, 403):
                    row["error"] = ("This key cannot read this layer. Some layers "
                                    "have to be added to the key's licence on "
                                    "data.linz.govt.nz.")
                elif r.status_code != 200:
                    row["error"] = r.text[:200]
                else:
                    reached += 1
                    feats = r.json().get("features") or []
                    row["features"] = len(feats)
                    if feats:
                        props = feats[0].get("properties") or {}
                        # Geometry is megabytes and useless in a table.
                        row["fields"] = {k: _short(v) for k, v in sorted(props.items())
                                         if k.lower() not in ("shape", "geom", "geometry")}
                        for k, v in props.items():
                            if k not in seen and v not in (None, ""):
                                seen[k] = (layer["name"], v)
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            out["layers"].append(row)

        out["reached"] = reached
        # A scorecard drawn from four failed requests reads as "none of this is
        # available free", which is the opposite of what it means.
        if reached == 0:
            out["coverage"] = []
            return out

        for beta, label, candidates in MODEL_INPUTS:
            hit = None
            for field, (layer_name, value) in seen.items():
                low = field.lower()
                if any(c in low for c in candidates):
                    hit = {"field": field, "layer": layer_name, "value": _short(value)}
                    break
            out["coverage"].append({"beta": beta, "input": label, "found": hit})

    return out


def _short(v: object) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= 120 else s[:117] + "…"
