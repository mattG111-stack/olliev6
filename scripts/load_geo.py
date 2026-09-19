#!/usr/bin/env python3
"""Load the land and the pipes. One command each.

    "but its free its just easier to do it that way"

It was a fair point, and it was fair because the easy path was not finished.
The fetcher and the loaders existed and nothing called them, so "use the
council's service" meant writing code and "scrape a property page" meant
writing code, and one of those was already familiar. This is the difference.

    # START HERE. Finds the water, wastewater and stormwater layers on a
    # service by name, checks each one answers, and prints the settings to
    # paste into Railway. Nothing to load afterwards — the property pages ask
    # the service directly.
    python scripts/load_geo.py autowire \\
      "https://wslgis.water.co.nz/arcgis/rest/services/Services/WaterComb/MapServer"

    # what's in a service, and what one layer holds, when autowire misses one.
    # The number on the end of a layer URL is not guessable and "water mains"
    # is layer 9 on one service and 21 on the next.
    python scripts/load_geo.py probe \\
      "https://wslgis.water.co.nz/arcgis/rest/services/Services/WaterComb/MapServer"
    python scripts/load_geo.py probe \\
      "https://wslgis.water.co.nz/arcgis/rest/services/Services/WaterComb/MapServer/9"

    # pull one suburb's pipes out to a file, to look at or to send on
    python scripts/load_geo.py sample \\
      "https://.../WaterComb/MapServer/9" riverhead 800 riverhead-water.geojson

    # stormwater, straight from the council's own service
    python scripts/load_geo.py pipes stormwater \\
      "https://services1.arcgis.com/n4yPwebTjJCmXB6W/arcgis/rest/services/Stormwater_Connection/FeatureServer/0/query?outFields=*&where=1=1&f=geojson"

    # water and wastewater, from Watercare's own server
    python scripts/load_geo.py pipes water \\
      "https://wslgis.water.co.nz/arcgis/rest/services/Services/WaterComb/MapServer/9/query"

    # or from a file somebody exported
    python scripts/load_geo.py pipes wastewater ./wastewater.geojson

    # the land
    python scripts/load_geo.py parcels ./auckland-parcels.geojson
    python scripts/load_geo.py zones   ./unitary-plan-zones.geojson

    # and the distances onto every section, after any of the above
    python scripts/load_geo.py distances

Every command prints what it found AND what it did not. A load that silently
filled nothing looks exactly like one that worked, until somebody searches days
later and gets an empty screen.

Nothing here guesses. A file in the wrong projection is refused by name rather
than loaded into the Tasman Sea; a fetch that stopped at a page limit says so
rather than passing off a fraction of the city as a network.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

USAGE = __doc__


def _die(msg: str, code: int = 2) -> None:
    print(f"\n  {msg}\n", file=sys.stderr)
    raise SystemExit(code)


def _features(source: str, label: str) -> list[dict]:
    """Features from a URL or a file, whichever this is."""
    if source.startswith("http://") or source.startswith("https://"):
        from app.arcgis import fetch_layer

        # A layer URL without /query on it is the commonest way to get a page
        # of HTML back and a puzzling "did not return JSON".
        head = source.split("?", 1)[0].rstrip("/")
        if not head.endswith("/query"):
            tail = source.split("?", 1)[1] if "?" in source else ""
            source = head + "/query" + (f"?{tail}" if tail else "")
        print(f"  fetching {label} from the service…")
        got = fetch_layer(source)
        if got.info:
            for line in got.info.lines():
                print(f"    {line}")
        for line in got.lines():
            print(f"    {line}")
        if not got.complete:
            # Deliberately fatal. A partial network is worse than none: every
            # distance computed from it is wrong and nothing on any screen says
            # which part of the city it covers.
            _die("Refusing to load a partial layer. Fix the query and re-run.")
        return got.features

    if source.lower().endswith((".json", ".geojson")):
        from app.parcels import load_geojson

        return load_geojson(source)

    if source.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        from app.services_network import rows_to_features

        ws = openpyxl.load_workbook(source, read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            _die(f"{source} is empty.")
        hdr = [str(h) if h is not None else "" for h in rows[0]]
        dicts = [dict(zip(hdr, r)) for r in rows[1:]]
        feats = rows_to_features(dicts)
        placed = sum(1 for f in feats if f["geometry"].get("type"))
        if not placed:
            _die(
                f"{source} has {len(dicts):,} rows and no coordinates in any of "
                f"them. A spreadsheet exported from ArcGIS as an ATTRIBUTE "
                f"TABLE drops the shape — every column about the pipe survives "
                f"and the pipe's location does not. Re-export with geometry, or "
                f"use the service URL, which carries it.")
        return feats

    _die(f"Don't know how to read {source}. Give a URL, a .geojson, or a .xlsx.")
    return []


# A few places by name, so a sample does not start with looking up coordinates.
PLACES = {
    "riverhead": (-36.7700, 174.5850),
    "kumeu": (-36.7690, 174.5480),
    "huapai": (-36.7620, 174.5390),
    "greylynn": (-36.8580, 174.7450),
    "grey lynn": (-36.8580, 174.7450),
    "papakura": (-37.0660, 174.9440),
    "ellerslie": (-36.8980, 174.8100),
}


def _sample(layer_url: str, where: str, radius_m: float, out: str) -> int:
    """Pull the pipes around one place and write them to a GeoJSON file.

    For getting a real extract out of a service and into somebody else's hands
    — a support conversation, a check that the field names are what we think,
    or a drawing of an actual suburb rather than an invented one. It writes what
    the service returned, unchanged, so nothing here can quietly become the
    answer to a question about real ground.
    """
    import json as _json

    import httpx

    from app.arcgis import fetch_near

    key = where.strip().lower()
    if key in PLACES:
        lat, lng = PLACES[key]
    else:
        try:
            lat, lng = (float(v) for v in where.split(",", 1))
        except ValueError:
            _die(f"Don't know where {where!r} is. Give a lat,lng, or one of: "
                 + ", ".join(sorted(PLACES)))

    def get(u: str) -> str:
        r = httpx.get(u, timeout=60.0)
        r.raise_for_status()
        return r.text

    head = layer_url.split("?", 1)[0].rstrip("/")
    if not head.endswith("/query"):
        head += "/query"

    print(f"  {where} at {lat:.4f}, {lng:.4f} — {int(radius_m)} m around it…")
    got = fetch_near(head, lat, lng, radius_m, get=get)
    for line in got.lines():
        print(f"    {line}")
    if not got.features:
        _die("Nothing came back. Check the layer number with `probe`, and that "
             "this place is inside the layer's coverage.")

    with open(out, "w") as fh:
        _json.dump({"type": "FeatureCollection", "features": got.features,
                    "centre": [lat, lng], "radius_m": radius_m}, fh)
    print(f"\n  wrote {out}  ({len(got.features):,} features)")
    if not got.complete:
        print("  NOTE: this is not everything in that radius — see above.")
    return 0


def _autowire(service_urls: list[str]) -> int:
    """Find the network layers on one or more services and print the settings.

    The step that was left to a person, and should not have been. Reading sixty
    layer names off a service listing and picking numbers by hand is how you get
    a number that is silently wrong the day the operator renumbers — the layer
    returns nothing, the map shows a suburb with no water, and nothing anywhere
    says which of those two things happened.

    Every layer is checked before it is offered: named, counted and confirmed to
    answer. A URL that is printed here has been asked a real question.
    """
    import httpx

    from app.arcgis import find_layers, probe

    def get(u: str) -> str:
        r = httpx.get(u, timeout=60.0)
        r.raise_for_status()
        return r.text

    found: dict[str, list[str]] = {}
    for base in service_urls:
        base = base.split("?", 1)[0].rstrip("/")
        print(f"\n  {base}")
        try:
            groups = find_layers(base, get)
        except Exception as exc:                       # noqa: BLE001
            print(f"    could not read it: {exc}")
            continue
        if not groups:
            print("    no water, wastewater or stormwater layers recognised "
                  "here. Run `probe` on it to see the full list.")
            continue
        for kind in sorted(groups):
            for lid, name in groups[kind]:
                url = f"{base}/{lid}/query"
                info = probe(url, get)
                shape = info.geometry_type.replace("esriGeometry", "") or "?"
                print(f"    [{kind:10}] {lid:>3}  {name}  ({shape})")
                found.setdefault(kind, []).append(url)

    if not found:
        _die("Nothing recognised. Check the service URL opens in a browser.")

    print("\n  Set these — one line per kind, several layers comma-separated:\n")
    for kind in ("water", "wastewater", "stormwater"):
        if found.get(kind):
            print(f"    SERVICE_URL_{kind.upper()}={','.join(found[kind])}\n")
    print("  Paste them into Railway's variables and the property pages draw "
          "the network. Nothing needs loading.\n")
    return 0


def _probe(url: str) -> int:
    """Say what a service holds, or what one layer holds. Reads nothing in.

    Deliberately separate from loading. Pointing a loader at a guessed layer
    number and seeing what turns up costs a full fetch and fills the database
    with whatever it was; asking first costs one request.
    """
    import httpx

    from app.arcgis import list_layers, probe

    def get(u: str) -> str:
        r = httpx.get(u, timeout=60.0)
        r.raise_for_status()
        return r.text

    base = url.split("?", 1)[0].rstrip("/")
    head, _, last = base.rpartition("/")
    is_layer = last.isdigit() or base.endswith("/query")

    if not is_layer:
        rows = list_layers(base, get)
        if not rows:
            _die(f"No layers listed at {base}. Check the URL opens in a browser "
                 f"— a service URL ends at MapServer or FeatureServer.")
        print(f"\n  {len(rows)} layer(s):\n")
        for lid, name in rows:
            print(f"    {lid:>4}  {name}")
        print("\n  Then probe the one you want:  load_geo.py probe "
              f"{base}/<id>\n")
        return 0

    query = base if base.endswith("/query") else base + "/query"
    info = probe(query, get)
    print()
    for line in info.lines():
        print(f"    {line}")
    print()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(USAGE)
        return 0

    what = argv[1].lower()
    region = os.environ.get("REGION", "Auckland")

    if what == "probe":
        if len(argv) < 3:
            _die("Usage: load_geo.py probe <service-or-layer-url>")
        return _probe(argv[2])

    if what == "autowire":
        if len(argv) < 3:
            _die("Usage: load_geo.py autowire <service-url> [<service-url> …]")
        return _autowire(argv[2:])

    if what == "sample":
        if len(argv) < 4:
            _die("Usage: load_geo.py sample <layer-url> <place|lat,lng> "
                 "[radius_m] [out.geojson]")
        return _sample(argv[2], argv[3],
                       float(argv[4]) if len(argv) > 4 else 600.0,
                       argv[5] if len(argv) > 5 else "sample.geojson")

    from app.db import SessionLocal

    with SessionLocal() as db:
        if what == "pipes":
            if len(argv) < 4:
                _die("Usage: load_geo.py pipes <stormwater|wastewater|water> <url-or-file>")
            kind, source = argv[2].lower(), argv[3]
            from app.services_network import KINDS, load_pipes

            if kind not in KINDS:
                _die(f"kind must be one of {', '.join(KINDS)}")
            rep = load_pipes(db, _features(source, kind), kind=kind, region=region)
            db.commit()
            print(f"\n  {kind}:")
            for line in rep.lines():
                print(f"    {line}")
            print("\n  Now run:  python scripts/load_geo.py distances")
            return 0

        if what == "parcels":
            if len(argv) < 3:
                _die("Usage: load_geo.py parcels <url-or-file>")
            from app.parcels import load_parcels

            rep = load_parcels(db, _features(argv[2], "parcels"), region=region)
            db.commit()
            for line in rep.lines():
                print(f"    {line}")
            return 0

        if what == "zones":
            if len(argv) < 3:
                _die("Usage: load_geo.py zones <url-or-file>")
            from app.parcels import apply_zones

            rep = apply_zones(db, _features(argv[2], "zones"), region=region)
            db.commit()
            print(f"    {rep.written:,} parcels stamped with a zone")
            if not rep.written:
                print("    NOTHING STAMPED — check the zone layer covers this "
                      "region and that its zone column is one of the names the "
                      "loader looks for.")
            return 0

        if what == "distances":
            from app.services_network import stamp_parcels

            counts = stamp_parcels(db, region=region)
            db.commit()
            for kind, n in counts.items():
                print(f"    {kind}: {n:,} sections have one within reach")
            if not any(counts.values()):
                print("    NOTHING MEASURED — either no network is loaded for "
                      "this region, or no parcels are.")
            return 0

    _die(f"Unknown command {what!r}. Run with --help.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
