"""Pull a layer straight from an ArcGIS service — FeatureServer or MapServer.

    ".../Stormwater_Connection/FeatureServer/0/query?outFields=*&where=1=1&f=geojson"
    ".../Services/WaterComb/MapServer/9/query?where=1=1&outFields=*&outSR=4326&f=json"

Better than the file route in two ways that matter. `f=geojson` is specified to
be WGS84, so the NZTM trap that would have put the whole network in the Tasman
Sea does not arise — the guard stays in place anyway, because "specified to be"
and "is" are different claims and the cost of being wrong is the entire layer.
And a server that fetches its own data can refresh it, where a spreadsheet is
correct on the day somebody exported it.

THE THING THAT SILENTLY TRUNCATES A NETWORK. A layer will not return more than
its `maxRecordCount`, typically one or two thousand. It does not fail when there
is more — it answers with a page and a quiet flag. A naive fetch of Auckland's
stormwater network therefore returns the first two thousand pipes and looks
exactly like a complete network, and every distance computed afterwards is
measured against a fraction of the city with nothing on screen to say so.

So this pages, and it pages IN A STABLE ORDER. Offset paging over an unordered
result is not paging: the server may return rows in a different order between
requests, and the pages then overlap and skip. Ordering by the object id costs
nothing and makes the offsets mean something.

AND THE SECOND TRUNCATION, WHICH A FEATURESERVER NEVER SHOWED US. Offset paging
is an OPTIONAL capability: the layer advertises it as
`advancedQueryCapabilities.supportsPagination`, and a MapServer on an older
ArcGIS Server — which is what a utility's own GIS usually is — does not have it.
A server without it does not reject `resultOffset`. It IGNORES it, and cheerfully
answers page one again. Offset paging against such a layer returns the same
thousand pipes over and over until the feature cap, and the cap is the only
reason it ever stops. So the capability is asked for, and where it is missing the
paging walks the object id instead — `OBJECTID > the last one seen` — which needs
no capability at all and cannot silently stand still.

COMPLETENESS IS ASKED FOR RATHER THAN INFERRED. "The last page came back short,
so that was all of it" is precisely the inference silent truncation defeats. The
layer will say how many rows match, in one cheap request, so the count is checked
against what arrived and a shortfall is named.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

# One page. Most servers cap below this and return what they can; asking for
# more than the cap is not an error, it is simply capped.
PAGE = 1000
# A whole region's network, with room over. Past this something is wrong with
# the query rather than the city being large.
MAX_FEATURES = 400_000
# Courtesy between pages. A public council endpoint is not ours to hammer.
PAUSE_S = 0.2


@dataclass
class LayerInfo:
    """What the layer says about itself, before a single feature is asked for.

    Worth a request of its own. Every decision below — which paging to use,
    whether GeoJSON is even on offer, whether what arrived is all of it — is
    answerable from here, and guessing any of them fails quietly.
    """
    name: str = ""
    geometry_type: str = ""
    object_id_field: str = "OBJECTID"
    max_record_count: int = 0
    supports_pagination: bool = True
    supports_geojson: bool = True
    fields: list[str] = field(default_factory=list)
    note: str = ""

    def lines(self) -> list[str]:
        out = [f"layer: {self.name or '(unnamed)'}  {self.geometry_type or '?'}"]
        out.append(f"object id: {self.object_id_field}   "
                   f"page cap: {self.max_record_count or 'unstated'}")
        out.append("offset paging: " + ("yes" if self.supports_pagination
                                        else "NO — walking the object id instead"))
        out.append("geojson: " + ("yes" if self.supports_geojson
                                  else "no — reading Esri JSON and converting"))
        if self.fields:
            out.append(f"{len(self.fields)} fields: "
                       + ", ".join(self.fields[:12])
                       + (" …" if len(self.fields) > 12 else ""))
        if self.note:
            out.append(self.note)
        return out


@dataclass
class FetchReport:
    """What came back, and whether it is all of it."""
    features: list[dict] = field(default_factory=list)
    pages: int = 0
    complete: bool = False
    note: str = ""
    expected: int | None = None
    info: LayerInfo | None = None

    def lines(self) -> list[str]:
        out = [f"{len(self.features):,} features in {self.pages} page(s)"]
        if self.expected is not None:
            out.append(f"the layer says it holds {self.expected:,}")
        out.append("complete" if self.complete
                   else "INCOMPLETE — this is not the whole layer")
        if self.note:
            out.append(self.note)
        return out


def _with(url: str, params: dict[str, str]) -> str:
    import urllib.parse

    split = urllib.parse.urlsplit(url)
    have = dict(urllib.parse.parse_qsl(split.query))
    have.update(params)
    return urllib.parse.urlunsplit(
        (split.scheme, split.netloc, split.path, urllib.parse.urlencode(have), ""))


def _http_get(u: str) -> str:
    import httpx

    r = httpx.get(u, timeout=20.0,
                  headers={"Accept": "application/geo+json"})
    r.raise_for_status()
    return r.text


def _layer_url(url: str) -> str:
    """The layer itself, given the query endpoint on it."""
    return url.rsplit("/query", 1)[0]


def probe(url: str, get) -> LayerInfo:
    """Ask the layer what it is and what it can do, before fetching anything.

    Every field here is asked rather than assumed. The object id is usually
    OBJECTID and is FID or OBJECTID_1 often enough that hard-coding it would
    break paging on exactly the layers that most need it; and `supportsPagination`
    is the difference between paging and standing still.
    """
    info = LayerInfo()
    try:
        meta = json.loads(get(_with(_layer_url(url), {"f": "json"})))
    except Exception as exc:                           # noqa: BLE001
        info.note = (f"Could not read the layer description ({exc}). Falling "
                     f"back to the safe assumptions: object id paging, Esri "
                     f"JSON.")
        info.supports_pagination = False
        return info
    if isinstance(meta, dict) and meta.get("error"):
        err = meta["error"]
        info.note = f"The server refused the layer description: {err.get('message', err)}"
        info.supports_pagination = False
        return info

    info.name = str(meta.get("name") or "")
    info.geometry_type = str(meta.get("geometryType") or "")
    info.object_id_field = str(meta.get("objectIdField") or "OBJECTID")
    info.max_record_count = int(meta.get("maxRecordCount") or 0)
    adv = meta.get("advancedQueryCapabilities") or {}
    # Absent means the server is old enough not to describe the capability,
    # and a server old enough not to describe it is a server that does not
    # have it. Defaulting this to True is the silent-repeat-page bug.
    info.supports_pagination = bool(adv.get("supportsPagination"))
    formats = str(meta.get("supportedQueryFormats") or "")
    info.supports_geojson = "geojson" in formats.lower()
    info.fields = [str(f.get("name")) for f in (meta.get("fields") or [])
                   if f.get("name")]
    return info


def list_layers(service_url: str, get) -> list[tuple[int, str]]:
    """Every layer in a MapServer or FeatureServer, as (id, name).

    A service URL is not a layer URL, and the number on the end of a layer URL
    is not guessable — "the water mains" might be 9 on one service and 21 on
    the next. So it is read off the service rather than picked.
    """
    base = service_url.split("?", 1)[0].rstrip("/")
    for tail in ("/query",):
        if base.endswith(tail):
            base = base[: -len(tail)]
    # Trim a layer index if one was given, so a layer URL works here too.
    head, _, last = base.rpartition("/")
    if last.isdigit():
        base = head
    try:
        meta = json.loads(get(_with(base, {"f": "json"})))
    except Exception:                                  # noqa: BLE001
        return []
    out = []
    for group in ("layers", "tables"):
        for lyr in (meta.get(group) or []):
            if lyr.get("id") is not None:
                out.append((int(lyr["id"]), str(lyr.get("name") or "")))
    return out


# What an operator calls a layer. Read off real service listings rather than
# invented: "Water Main", "Wastewater Manhole", "Stormwater Pipe", "Sewer Main".
# Order matters — the first list to match wins, so "Stormwater" is tested before
# "Water" or every stormwater layer would be classed as water.
_KIND_WORDS = (
    ("stormwater", ("stormwater", "storm water", "storm main", "storm pipe")),
    ("wastewater", ("wastewater", "waste water", "sewer", "sewerage", "foul")),
    ("water", ("water main", "watermain", "potable", "reticulation", "water")),
)
# Layers that are the NETWORK, as against everything else a utility publishes on
# the same service — catchments, zones, consents, aerial imagery, easements.
#
# FITTINGS ARE DELIBERATELY NOT HERE. A hydrant and a valve are on the water
# network and neither is something a development can tie into. Offering them
# would put a point outside almost every house and let it become "the nearest
# water" — advice that is wrong in the one direction that costs money, because
# it says connect here to somewhere you cannot connect. Manholes stay: a manhole
# IS where a wastewater connection gets made, which is why every plan draws one.
_NETWORK_WORDS = ("main", "pipe", "manhole", "node", "chamber", "lateral",
                  "connection", "sewer", "reticulation")


def classify(name: str) -> str | None:
    """Which of our three kinds a layer name describes, if any.

    The point is to stop anybody hand-picking layer numbers off a list of
    sixty. A number picked by eye is a number that silently becomes wrong when
    the operator renumbers their service, and nothing on any screen would say
    so — the layer would simply return nothing and the map would look like a
    suburb with no water.
    """
    low = (name or "").lower()
    if not any(w in low for w in _NETWORK_WORDS):
        return None
    for kind, words in _KIND_WORDS:
        if any(w in low for w in words):
            return kind
    return None


def find_layers(service_url: str, get) -> dict[str, list[tuple[int, str]]]:
    """The network layers on a service, grouped by kind.

    Several per kind is normal and is the point: the mains and the manholes are
    different layers, and a viewer that draws both is drawing two of them.
    """
    out: dict[str, list[tuple[int, str]]] = {}
    for lid, name in list_layers(service_url, get):
        kind = classify(name)
        if kind:
            out.setdefault(kind, []).append((lid, name))
    return out


def _count(url: str, get) -> int | None:
    """How many rows match, straight from the server.

    One cheap request, and it turns "complete" from an inference into a check.
    Not every layer supports it; None means it did not answer, not zero.
    """
    try:
        data = json.loads(get(_with(url, {"f": "json", "where": "1=1",
                                          "returnCountOnly": "true"})))
    except Exception:                                  # noqa: BLE001
        return None
    n = data.get("count") if isinstance(data, dict) else None
    return int(n) if isinstance(n, int) else None


def _from_esri(data: dict) -> list[dict] | None:
    """Esri JSON features as GeoJSON. None when they are not in degrees.

    Esri JSON says which projection it is in, which GeoJSON does not have to
    because it is always WGS84. So the check is possible here and it is worth
    making: 2193 is NZTM2000 and 102100 is Web Mercator, and a layer in either
    would load silently and be wrong everywhere.
    """
    wkid = ((data.get("spatialReference") or {}).get("latestWkid")
            or (data.get("spatialReference") or {}).get("wkid"))
    if wkid not in (4326, None):
        return None

    out = []
    for f in data.get("features") or []:
        g = f.get("geometry") or {}
        props = f.get("attributes") or {}
        if "paths" in g:
            geom = {"type": "MultiLineString", "coordinates": g["paths"]}
        elif "rings" in g:
            geom = {"type": "Polygon", "coordinates": g["rings"]}
        elif "x" in g and "y" in g:
            geom = {"type": "Point", "coordinates": [g["x"], g["y"]]}
        else:
            geom = {}
        out.append({"type": "Feature", "properties": props, "geometry": geom})
    return out


def fetch_near(url: str, lat: float, lng: float, radius_m: float,
               *, get=None) -> FetchReport:
    """The features around ONE point, asked for directly.

    THIS IS WHAT THE SERVICE IS FOR, and it took a wrong turn to see it. The
    fetch above downloads a region's whole network so we can measure against it
    later, which is the shape you need when the source is a file somebody
    exports. A live service does not need it: a property page has a point and a
    radius, and the server will answer that question in one request.

    Three things follow, and they are the reasons to prefer this. Nothing has to
    be loaded before a property can show its mains. The answer is current rather
    than as current as the last import. And the reply is a few dozen pipes
    instead of a hundred thousand, so the paging, the cap and the completeness
    check above simply do not arise.

    The filter is an envelope rather than a circle because every ArcGIS service
    supports envelopes and the exact distances are measured afterwards anyway —
    a box slightly larger than the circle costs a few extra pipes and rules out
    nothing that should have been found.
    """
    if get is None:
        get = _http_get

    # Degrees per metre. Latitude is very nearly constant; longitude narrows
    # with the cosine, and at Auckland's latitude ignoring that would make the
    # box 20% short east-west — a main just outside the box is reported as no
    # main at all, which reads as a servicing problem rather than a query one.
    dlat = radius_m / 110_574.0
    dlng = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return fetch_envelope(url, lng - dlng, lat - dlat, lng + dlng, lat + dlat,
                          get=get)


def fetch_envelope(url: str, west: float, south: float, east: float,
                   north: float, *, get=None) -> FetchReport:
    """The features inside a rectangle of the map.

    The shape the property page actually wants. A developer looking at a site
    pans and zooms, and what they expect to see is the network across the
    streets in front of them — not a circle round a pin that ends mid-road.
    So the view's own bounds are the query, and moving the map asks again.
    """
    if get is None:
        get = _http_get

    rep = FetchReport()
    params = {
        "f": "geojson",
        "outFields": "*",
        "outSR": "4326",
        # The envelope is given in the SAME coordinates it is read in. Without
        # inSR the server assumes the layer's own projection, and -36.9 read as
        # NZTM metres is a box in the Southern Ocean that contains nothing —
        # an empty answer with no error on it, which looks exactly like a
        # property with no services near it.
        "inSR": "4326",
        "geometryType": "esriGeometryEnvelope",
        "geometry": f"{west},{south},{east},{north}",
        "spatialRel": "esriSpatialRelIntersects",
    }
    try:
        data = json.loads(get(_with(url, params)))
    except ValueError:
        rep.note = "The service did not return JSON."
        return rep
    except Exception as exc:                           # noqa: BLE001
        rep.note = f"Could not reach the service: {exc}"
        return rep

    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        rep.note = f"The service refused: {err.get('message', err)}"
        return rep

    got = data.get("features") or []
    if got and "type" not in ((got[0].get("geometry")) or {"type": None}):
        got = _from_esri(data)
        if got is None:
            rep.note = ("The service answered in a projected coordinate system "
                        "rather than degrees — these would land in the Tasman "
                        "Sea.")
            return rep
    rep.features = got
    rep.pages = 1
    # A single request around a point either answered or it did not. There is
    # no paging to be short of — but a full page means the radius caught more
    # than the server will hand over at once, and the far ones are missing.
    if bool(data.get("exceededTransferLimit")):
        rep.note = ("The service capped this answer, so some mains near this "
                    "property are missing. Use a smaller radius.")
        return rep
    rep.complete = True
    return rep


def fetch_layer(url: str, *, get=None, page: int = PAGE,
                max_features: int = MAX_FEATURES) -> FetchReport:
    """Every feature in an ArcGIS layer, as GeoJSON features.

    `get` is a callable taking a URL and returning the body, so this is
    testable without a network and so the caller decides the HTTP client.
    """
    if get is None:
        get = _http_get

    rep = FetchReport()
    rep.info = info = probe(url, get)
    rep.expected = _count(url, get)
    oid = info.object_id_field
    # A layer that caps below our page size decides the page size. Asking for a
    # thousand from a server that gives five hundred is not an error, but the
    # object-id walk needs to know what a full page actually looks like.
    if info.max_record_count:
        page = min(page, info.max_record_count)

    offset = 0
    last_id = None
    while True:
        params = {
            "f": "geojson" if info.supports_geojson else "json",
            "outFields": "*",
            "outSR": "4326",
            # Without this the paging is meaningless: an unordered result may
            # come back in a different order each request, so pages overlap and
            # skip and the network quietly loses pipes.
            "orderByFields": oid,
            "resultRecordCount": str(page),
        }
        if info.supports_pagination:
            params["where"] = "1=1"
            params["resultOffset"] = str(offset)
        else:
            # No offset support. Walk the object id instead — the server cannot
            # ignore a WHERE clause the way it quietly ignores resultOffset, so
            # this either advances or ends, and never repeats a page for ever.
            params["where"] = "1=1" if last_id is None else f"{oid}>{last_id}"
        body = get(_with(url, params))
        try:
            data = json.loads(body)
        except ValueError:
            rep.note = ("The endpoint did not return JSON. Check the URL opens "
                        "in a browser and that f=geojson is supported.")
            return rep
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            rep.note = f"The server refused: {err.get('message', err)}"
            return rep

        got = (data or {}).get("features") or []
        # A server that does not do GeoJSON answers in Esri's own JSON instead,
        # without complaining — different geometry shape, and in the SERVICE'S
        # projection rather than WGS84. For a New Zealand council service that
        # is NZTM2000, which is the whole-network-in-the-Tasman fault arriving
        # through a door the guard is not watching. Converted here, and refused
        # outright if it is not in degrees.
        if got and "geometry" in got[0] and "type" not in (got[0].get("geometry") or {}):
            got = _from_esri(data)
            if got is None:
                rep.note = (
                    "The server answered in Esri JSON in a projected coordinate "
                    "system rather than GeoJSON in degrees. Add f=geojson to the "
                    "URL — that is specified to come back in WGS84, and without "
                    "it the whole layer would load into the Tasman Sea.")
                return rep
        rep.features.extend(got)
        rep.pages += 1

        if len(rep.features) >= max_features:
            rep.note = (f"Stopped at the {max_features:,} feature cap. Either "
                        f"the layer is larger than a region's network should "
                        f"be, or the query is wrong.")
            return rep
        # Two ways a server says "there is more": the flag, and a full page.
        # Trusting only the flag misses servers that do not set it; trusting
        # only the page size loops one extra time, which is harmless.
        more = bool(data.get("exceededTransferLimit")) or len(got) == page
        if not got or not more:
            return _finish(rep)

        if info.supports_pagination:
            offset += len(got)
        else:
            nxt = _max_id(got, oid)
            if nxt is None:
                rep.note = (
                    f"This layer does not support offset paging, so the fetch "
                    f"walks {oid} — and {oid} is not among the fields that came "
                    f"back. Without it the next page cannot be asked for, and "
                    f"continuing would re-fetch page one for ever.")
                return rep
            if last_id is not None and nxt <= last_id:
                # The id did not advance. Either the order is not what we asked
                # for or the server ignored the WHERE, and either way the next
                # request returns this same page. Stop and say so rather than
                # spin to the cap.
                rep.note = (f"Paging stopped advancing at {oid} {nxt}. The "
                            f"server is not honouring the order or the filter, "
                            f"so this is not the whole layer.")
                return rep
            last_id = nxt
        if PAUSE_S:
            time.sleep(PAUSE_S)


def _max_id(features: list[dict], oid: str) -> int | None:
    """The highest object id in a page, for the walk to carry on from."""
    best = None
    for f in features:
        v = (f.get("properties") or {}).get(oid)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        v = int(v)
        if best is None or v > best:
            best = v
    return best


def _finish(rep: FetchReport) -> FetchReport:
    """Complete means the count agrees, not merely that the pages ran out.

    "The last page came back short" is the very inference a truncating server
    satisfies. Where the layer told us its count, that is the answer.
    """
    got = len(rep.features)
    if rep.expected is None:
        rep.complete = True
        return rep
    if got >= rep.expected:
        rep.complete = True
        return rep
    rep.note = (f"The layer says it holds {rep.expected:,} features and only "
                f"{got:,} arrived. Something is dropping rows — do not load "
                f"this as a network.")
    return rep
