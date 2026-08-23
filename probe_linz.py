#!/usr/bin/env python3
"""What can we actually get for free? Ask LINZ and find out.

Run this against a real Auckland address with a real LINZ Data Service key. It
queries every free layer that could plausibly carry a valuation input, prints
EVERY field each one returns, and then scores those fields against the nine
features the pricing model takes.

The point is not to confirm what I think is there. It is to print what IS there,
including the fields nobody expected, so the decision about what to buy is made
against the data rather than against anyone's recollection of a schema. Where a
layer turns out to carry something useful, that is one less thing on the invoice.

    export LINZ_API_KEY=...            # data.linz.govt.nz -> your key
    python3 probe_linz.py "12 Example Road, Remuera"
    python3 probe_linz.py -36.8790 174.7770

Standard library only, on purpose: no pip install, no venv, no virtualenv
mismatch on someone's laptop at nine at night.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

KEY = (os.environ.get("LINZ_API_KEY") or "").strip()
WFS = "https://data.linz.govt.nz/services;key={key}/wfs"
TIMEOUT = 20

# The free layers worth asking. `geom` is the geometry column name, which differs
# per layer and is the usual reason a spatial filter silently returns nothing.
LAYERS = [
    {
        "id": "layer-123113",
        "name": "NZ Addresses",
        "geom": "shape",
        "why": "resolves an address, and carries suburb / town / postcode",
        "mode": "near",
    },
    {
        "id": "layer-50772",
        "name": "NZ Primary Parcels",
        "geom": "shape",
        "why": "land area (beta4) + legal description",
        "mode": "contains",
    },
    {
        "id": "layer-50804",
        "name": "NZ Property Titles",
        "geom": "shape",
        "why": "estate type -> fee simple / cross-lease / unit (beta10-13)",
        "mode": "contains",
    },
    {
        "id": "layer-101292",
        "name": "NZ Building Outlines",
        "geom": "shape",
        "why": "roof footprint - a floor-area sanity check, not floor area",
        "mode": "contains",
    },
]

# The model's inputs, and what would satisfy each. Filled in from what actually
# comes back, not from what the layer documentation claims.
MODEL_INPUTS = [
    ("beta2  CV",            ["capital_value", "cv", "rating_value"]),
    ("beta3  Floor area",    ["floor_area", "floorarea", "building_floor_area"]),
    ("beta4  Land area",     ["calc_area", "survey_area", "land_area", "parcel_area"]),
    ("beta5  Bedrooms",      ["bedrooms", "beds", "num_bedrooms"]),
    ("beta6  Bathrooms",     ["bathrooms", "baths", "num_bathrooms"]),
    ("beta7  Car spaces",    ["carspaces", "garages", "car_spaces"]),
    ("beta8  Age",           ["year_built", "decade_built", "construction_year"]),
    ("beta10 Title type",    ["estate_description", "type", "estate", "tenure"]),
    ("suburb / district",    ["suburb_locality", "suburb", "town_city", "territorial_authority"]),
]

GREEN, AMBER, RED, DIM, BOLD, OFF = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m")
if not sys.stdout.isatty():
    GREEN = AMBER = RED = DIM = BOLD = OFF = ""


def fetch(url: str) -> tuple[int, dict | str]:
    """GET and parse. Returns (status, body). Never raises — a layer this key
    cannot see is a finding, not a crash, and the run must reach the others."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw[:400]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        return e.code, body
    except Exception as e:                       # DNS, TLS, timeout
        return 0, f"{type(e).__name__}: {e}"


def query(layer: dict, lat: float, lng: float) -> tuple[int, list[dict], str]:
    """One layer, one point. `contains` for polygons, a small bbox for points."""
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer["id"],
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": "3",
    }
    if layer["mode"] == "contains":
        # CQL takes POINT in x y order — lon lat. Getting this backwards returns
        # an empty result rather than an error, which is the most expensive kind
        # of mistake to debug.
        params["cql_filter"] = f"Contains({layer['geom']},POINT({lng} {lat}))"
    else:
        d = 0.0004                                # ~40 m — one property, not a street
        params["cql_filter"] = (
            f"BBOX({layer['geom']},{lng - d},{lat - d},{lng + d},{lat + d})")

    url = WFS.format(key=KEY) + "?" + urllib.parse.urlencode(params)
    status, body = fetch(url)
    if status != 200 or not isinstance(body, dict):
        return status, [], str(body)[:300]
    return status, (body.get("features") or []), ""


def geocode(text: str) -> tuple[float, float, str] | None:
    """Address text -> a point, using LINZ's own address layer.

    Deliberately not Google. If the free layer cannot find the address, that is
    something worth knowing before building a product on it.
    """
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": "layer-123113", "outputFormat": "application/json",
        "srsName": "EPSG:4326", "count": "1",
        "cql_filter": f"full_address ILIKE '%{text.replace(chr(39), '')}%'",
    }
    url = WFS.format(key=KEY) + "?" + urllib.parse.urlencode(params)
    status, body = fetch(url)
    if status != 200 or not isinstance(body, dict):
        print(f"{RED}Address lookup failed ({status}){OFF}: {str(body)[:200]}")
        return None
    feats = body.get("features") or []
    if not feats:
        return None
    p = feats[0].get("properties") or {}
    coords = ((feats[0].get("geometry") or {}).get("coordinates") or [None, None])
    if coords[0] is None:
        return None
    return float(coords[1]), float(coords[0]), p.get("full_address") or text


def main() -> int:
    if not KEY:
        print(f"{RED}No LINZ_API_KEY set.{OFF}\n")
        print("  Get one free at https://data.linz.govt.nz  (register -> API keys)")
        print("  Then:  export LINZ_API_KEY=your-key\n")
        print("Note this is the LINZ DATA SERVICE key, which is a different key")
        print("from LINZ Basemaps (the aerial imagery one).")
        return 2

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    if len(args) >= 2 and _isnum(args[0]) and _isnum(args[1]):
        lat, lng, label = float(args[0]), float(args[1]), f"{args[0]}, {args[1]}"
    else:
        text = " ".join(args)
        print(f"{DIM}Looking up {text!r} in LINZ NZ Addresses…{OFF}")
        found = geocode(text)
        if not found:
            print(f"{RED}No address matched.{OFF} Try a lat lng instead, "
                  f"or check the spelling against data.linz.govt.nz.")
            return 1
        lat, lng, label = found
        print(f"{GREEN}Matched{OFF} {label}  ->  {lat:.5f}, {lng:.5f}")

    print(f"\n{BOLD}Probing {label}{OFF}\n")

    seen: dict[str, tuple[str, object]] = {}      # field -> (layer, value)
    reached = 0                                   # layers that actually answered

    for layer in LAYERS:
        print(f"{BOLD}{layer['name']}{OFF}  {DIM}{layer['id']} — {layer['why']}{OFF}")
        status, feats, err = query(layer, lat, lng)

        if status == 0:
            print(f"  {RED}unreachable{OFF}  {err}\n")
            continue
        if status == 401 or status == 403:
            print(f"  {RED}{status} — this key cannot read this layer{OFF}")
            print(f"  {DIM}Some layers need to be added to your key's licence "
                  f"on data.linz.govt.nz.{OFF}\n")
            continue
        if status != 200:
            print(f"  {RED}HTTP {status}{OFF}  {err}\n")
            continue
        if not feats:
            print(f"  {AMBER}200, but nothing here.{OFF} "
                  f"{DIM}Either no coverage at this point, or the geometry "
                  f"column is not {layer['geom']!r}.{OFF}\n")
            continue

        reached += 1
        props = feats[0].get("properties") or {}
        print(f"  {GREEN}{len(feats)} feature(s){OFF}, {len(props)} fields:")
        for k, v in sorted(props.items()):
            if k not in seen:
                seen[k] = (layer["name"], v)
            shown = str(v)
            if len(shown) > 68:
                shown = shown[:65] + "…"
            print(f"    {k:<28} {DIM}{shown}{OFF}")
        print()

    # ── the scorecard ────────────────────────────────────────────────────────
    #
    # Only printed if something actually answered. A scorecard of nine red
    # crosses drawn from four failed requests reads as "none of this is
    # available free", which is the opposite of what it means.
    if reached == 0:
        print(f"{RED}Nothing answered, so there is nothing to score.{OFF}\n")
        print("  Reaching none of the layers usually means one of:")
        print("    - the key is wrong or has been revoked")
        print("    - no internet from wherever this is running")
        print("    - a proxy or firewall between here and data.linz.govt.nz")
        print(f"\n  {DIM}Do NOT read this as 'the data is not available'. "
              f"It means the question was never asked.{OFF}")
        return 1

    print(f"{BOLD}What that covers of the model's inputs{OFF}\n")
    covered = 0
    for name, candidates in MODEL_INPUTS:
        hit = None
        for field, (layer_name, value) in seen.items():
            if field.lower() in candidates or any(c in field.lower() for c in candidates):
                if value not in (None, ""):
                    hit = (field, layer_name, value)
                    break
        if hit:
            covered += 1
            field, layer_name, value = hit
            shown = str(value)[:34]
            print(f"  {GREEN}✓{OFF} {name:<22} {field} = {shown}  {DIM}({layer_name}){OFF}")
        else:
            print(f"  {RED}✗{OFF} {name:<22} {DIM}not in any free layer — buy it "
                  f"or ask the owner{OFF}")

    print(f"\n{BOLD}{covered} of {len(MODEL_INPUTS)}{OFF} available free.")
    if covered < len(MODEL_INPUTS):
        print(f"{DIM}The rest come from QV (capital value) and an attribute feed "
              f"— CoreLogic or Valocity — or from asking the owner to confirm "
              f"their own details.{OFF}")
    print(f"\n{DIM}Every field printed above is free under CC-BY. If something "
          f"useful showed up that is not on the scorecard, say so — the "
          f"scorecard only knows the names it was told to look for.{OFF}")
    return 0


def _isnum(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    sys.exit(main())
