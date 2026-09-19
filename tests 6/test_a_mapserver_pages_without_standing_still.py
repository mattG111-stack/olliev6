"""Fetching from a MapServer, which is what a utility's own GIS usually is.

    "https://wslgis.water.co.nz/arcgis/rest/services/Services/WaterComb/
     MapServer/9/query?where=1=1&outFields=*&outSR=4326&f=json"
    "all we are after is the water and waste water pipes on the map"

The fetcher was written against a FeatureServer, and a MapServer differs in two
ways that are both silent.

OFFSET PAGING IS OPTIONAL. `advancedQueryCapabilities.supportsPagination` says
whether the layer has it, and an older ArcGIS Server does not. A server without
it DOES NOT REJECT `resultOffset` — it ignores it and returns page one again. So
offset paging against such a layer returns the same thousand pipes over and over
until the feature cap, and the cap is the only reason it ever stops. Every test
below with a `no_paging` server would pass just as well against the broken code
if the assertion were only "we got features".

AND GEOJSON IS OPTIONAL TOO. `supportedQueryFormats` says so. Ask a server that
does not do it for f=geojson and it answers in Esri JSON in ITS OWN PROJECTION,
which for a New Zealand utility is NZTM2000 — the whole-network-in-the-Tasman
fault, arriving through a door the guard was not watching.
"""
from __future__ import annotations

import json

from arcgis import fetch_layer, list_layers, probe

URL = ("https://wslgis.water.co.nz/arcgis/rest/services/Services/WaterComb"
       "/MapServer/9/query")


def _params(u: str) -> dict:
    import urllib.parse

    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))


def _pipe(oid: int) -> dict:
    return {"type": "Feature",
            "properties": {"OBJECTID": oid, "MATERIAL": "PVC"},
            "geometry": {"type": "LineString",
                         "coordinates": [[174.76, -36.85], [174.761, -36.851]]}}


class Server:
    """A MapServer with a fixed number of pipes and a switchable temperament."""

    def __init__(self, n: int, *, paging: bool, geojson: bool = True,
                 page: int = 100, counts: bool = True):
        self.n, self.paging, self.geojson = n, paging, geojson
        self.page, self.counts = page, counts
        self.calls: list[dict] = []

    def __call__(self, url: str) -> str:
        p = _params(url)
        self.calls.append(p)
        if not url.split("?", 1)[0].endswith("/query"):
            return json.dumps({
                "name": "Water Main", "geometryType": "esriGeometryPolyline",
                "objectIdField": "OBJECTID", "maxRecordCount": self.page,
                "supportedQueryFormats": "JSON, geoJSON" if self.geojson else "JSON",
                "fields": [{"name": "OBJECTID"}, {"name": "MATERIAL"}],
                "advancedQueryCapabilities": {"supportsPagination": self.paging},
            })
        if p.get("returnCountOnly") == "true":
            return json.dumps({"count": self.n} if self.counts else {})

        ids = list(range(1, self.n + 1))
        if self.paging:
            start = int(p.get("resultOffset") or 0)
        else:
            # THE WHOLE POINT. resultOffset is not supported, and rather than
            # complain the server ignores it. Only the WHERE moves us on.
            where = p.get("where") or "1=1"
            after = int(where.split(">", 1)[1]) if ">" in where else 0
            start = after
        take = min(int(p.get("resultRecordCount") or self.page), self.page)
        rows = [_pipe(i) for i in ids[start:start + take]]
        return json.dumps({"type": "FeatureCollection", "features": rows,
                           "exceededTransferLimit": start + take < self.n})


# ---- the thing that would have spun for ever ---------------------------------
def test_a_layer_without_offset_paging_still_fetches_the_whole_network():
    srv = Server(250, paging=False, page=100)
    got = fetch_layer(URL, get=srv, page=1000)
    assert got.complete, got.note
    assert len(got.features) == 250
    assert [f["properties"]["OBJECTID"] for f in got.features] == list(range(1, 251))


def test_it_walks_the_object_id_rather_than_the_offset():
    """The evidence, not just the count: an offset request to a server that
    ignores offsets looks identical to a working one until you read the URLs."""
    srv = Server(250, paging=False, page=100)
    fetch_layer(URL, get=srv, page=1000)
    queries = [c for c in srv.calls if "resultRecordCount" in c]
    assert all("resultOffset" not in c for c in queries), queries
    assert [c["where"] for c in queries] == ["1=1", "OBJECTID>100", "OBJECTID>200"]


def test_a_layer_with_offset_paging_still_uses_it():
    srv = Server(250, paging=True, page=100)
    got = fetch_layer(URL, get=srv, page=1000)
    assert got.complete and len(got.features) == 250
    offsets = [c["resultOffset"] for c in srv.calls if "resultOffset" in c]
    assert offsets == ["0", "100", "200"]


def test_pagination_absent_from_the_description_is_treated_as_absent():
    """An older server does not describe the capability at all. Reading a
    missing flag as "yes" is exactly the bug, so it must read as "no"."""
    def get(url: str) -> str:
        if not url.split("?", 1)[0].endswith("/query"):
            return json.dumps({"name": "Water Main", "objectIdField": "OBJECTID"})
        return json.dumps({"features": []})

    assert probe(URL, get).supports_pagination is False


def test_a_server_that_ignores_the_filter_is_caught_rather_than_looping():
    """If the id never advances, the next request returns this same page. That
    has to end and say so, not spin to the feature cap."""
    class Stuck(Server):
        def __call__(self, url: str) -> str:
            body = super().__call__(url)
            data = json.loads(body)
            if "features" in data and data["features"]:
                data["features"] = [_pipe(i) for i in range(1, self.page + 1)]
                data["exceededTransferLimit"] = True
            return json.dumps(data)

    got = fetch_layer(URL, get=Stuck(250, paging=False, page=100), page=1000)
    assert not got.complete
    assert "stopped advancing" in got.note
    assert len(got.features) < 400


# ---- completeness is checked, not inferred -----------------------------------
def test_a_short_layer_is_measured_against_what_the_layer_says_it_holds():
    """"The last page came back short, so that was all of it" is precisely the
    inference a truncating server satisfies."""
    class Truncating(Server):
        def __call__(self, url: str) -> str:
            p = _params(url)
            body = super().__call__(url)
            if "resultRecordCount" in p:
                data = json.loads(body)
                data["features"] = data["features"][:10]
                data["exceededTransferLimit"] = False
                return json.dumps(data)
            return body

    got = fetch_layer(URL, get=Truncating(250, paging=False, page=100), page=1000)
    assert not got.complete
    assert "250" in got.note and "10" in got.note
    assert got.expected == 250


def test_a_layer_that_will_not_give_a_count_is_not_called_incomplete():
    """No count is not zero, and a layer that cannot answer must not be
    permanently unloadable."""
    got = fetch_layer(URL, get=Server(250, paging=False, page=100, counts=False),
                      page=1000)
    assert got.expected is None
    assert got.complete and len(got.features) == 250


# ---- the projection door -----------------------------------------------------
def test_a_layer_that_does_not_do_geojson_is_asked_for_json_instead():
    srv = Server(50, paging=True, geojson=False, page=100)
    fetch_layer(URL, get=srv, page=1000)
    fmts = {c["f"] for c in srv.calls if "resultRecordCount" in c}
    assert fmts == {"json"}


def test_esri_json_in_nztm_is_refused_rather_than_loaded():
    """1.7 million is not a longitude. Nothing raises on it — the layer simply
    lands in the Tasman Sea and every distance after it is wrong."""
    def get(url: str) -> str:
        if not url.split("?", 1)[0].endswith("/query"):
            return json.dumps({"objectIdField": "OBJECTID",
                               "supportedQueryFormats": "JSON",
                               "advancedQueryCapabilities":
                                   {"supportsPagination": True}})
        if _params(url).get("returnCountOnly") == "true":
            return json.dumps({"count": 1})
        return json.dumps({
            "spatialReference": {"wkid": 2193, "latestWkid": 2193},
            "features": [{"attributes": {"OBJECTID": 1},
                          "geometry": {"paths": [[[1757000.0, 5920000.0],
                                                  [1757010.0, 5920010.0]]]}}]})

    got = fetch_layer(URL, get=get)
    assert not got.complete
    assert "Tasman" in got.note
    assert got.features == []


def test_the_page_size_never_exceeds_what_the_layer_will_give():
    """Asking for a thousand from a server that gives a hundred is not an
    error — but the walk has to know what a full page looks like or it calls a
    full page short and stops one page in."""
    srv = Server(250, paging=False, page=100)
    got = fetch_layer(URL, get=srv, page=1000)
    assert len(got.features) == 250
    assert {c["resultRecordCount"] for c in srv.calls
            if "resultRecordCount" in c} == {"100"}


# ---- finding the layer at all ------------------------------------------------
def test_the_layers_in_a_service_are_read_rather_than_guessed():
    """"Water mains" is layer 9 on one service and 21 on the next."""
    def get(url: str) -> str:
        assert "/MapServer?" in url or url.endswith("/MapServer")
        return json.dumps({"layers": [{"id": 0, "name": "Hydrant"},
                                      {"id": 9, "name": "Water Main"}],
                           "tables": [{"id": 30, "name": "Valve Notes"}]})

    base = ("https://wslgis.water.co.nz/arcgis/rest/services/Services/"
            "WaterComb/MapServer")
    assert list_layers(base, get) == [(0, "Hydrant"), (9, "Water Main"),
                                      (30, "Valve Notes")]


def test_a_layer_url_lists_its_service_too():
    """Paste the URL you have, not the one you should have trimmed to."""
    def get(url: str) -> str:
        return json.dumps({"layers": [{"id": 9, "name": "Water Main"}]})

    assert list_layers(URL, get) == [(9, "Water Main")]
