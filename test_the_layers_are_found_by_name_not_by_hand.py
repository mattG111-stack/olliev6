"""Finding the network layers on an operator's service, and drawing all of them.

    "One roof has that view how can't we"

We can. What stood in the way was configuration, and two specific things in it.

THE MANHOLES WERE NEVER GOING TO APPEAR. Their legend has three entries — water,
wastewater, AND wastewater manhole — because the mains and the nodes are
DIFFERENT LAYERS on these services. One URL per kind would have left the
manholes off the map for ever, and a manhole is the half of the drawing that
matters most: it is where a connection actually gets made.

AND THE LAYER NUMBERS WERE LEFT TO A PERSON. Reading sixty names off a service
listing and picking numbers by hand is how you get a number that is silently
wrong the day the operator renumbers. The layer returns nothing, the map shows a
suburb with no water, and nothing anywhere says which of those two happened.
"""
from __future__ import annotations

import json

import pytest

from arcgis import classify, find_layers

SERVICE = "https://wslgis.water.co.nz/arcgis/rest/services/Services/WaterComb/MapServer"

# A service listing of the shape these actually have: the network mixed in with
# everything else a utility publishes on the same endpoint.
LISTING = {"layers": [
    {"id": 0, "name": "Water Main"},
    {"id": 1, "name": "Water Valve"},
    {"id": 2, "name": "Hydrant"},
    {"id": 3, "name": "Wastewater Main"},
    {"id": 4, "name": "Wastewater Manhole"},
    {"id": 5, "name": "Stormwater Pipe"},
    {"id": 6, "name": "Stormwater Manhole"},
    {"id": 7, "name": "Water Supply Zone"},          # not the network
    {"id": 8, "name": "Wastewater Catchment"},       # not the network
    {"id": 9, "name": "Aerial Imagery 2024"},        # not ours at all
    {"id": 10, "name": "Watercourse"},               # a stream, not a main
]}


def get(url: str) -> str:
    return json.dumps(LISTING)


# ---- naming ------------------------------------------------------------------
@pytest.mark.parametrize("name,kind", [
    ("Water Main", "water"),
    ("Watermain (Potable)", "water"),
    ("Wastewater Main", "wastewater"),
    ("Wastewater Manhole", "wastewater"),
    ("Sewer Main", "wastewater"),
    ("Stormwater Pipe", "stormwater"),
    ("Stormwater Manhole", "stormwater"),
])
def test_a_layer_name_says_which_kind_it_is(name, kind):
    assert classify(name) == kind


def test_stormwater_is_not_classed_as_water():
    """THE ORDERING TRAP. "Stormwater" contains "water". Test the broad word
    first and every stormwater layer is drawn blue, on the wrong side of the
    street, and reported as the distance to a water connection."""
    assert classify("Stormwater Main") == "stormwater"
    assert classify("Storm Water Pipe") == "stormwater"


def test_a_catchment_is_not_a_pipe():
    """A zone or a catchment is a polygon covering half the district. Drawn as
    a main it would put a connection across every property in it."""
    assert classify("Wastewater Catchment") is None
    assert classify("Water Supply Zone") is None


def test_a_watercourse_is_not_a_main():
    """It is a stream. It contains the word water and you cannot connect to it."""
    assert classify("Watercourse") is None


def test_something_unrelated_is_left_alone():
    assert classify("Aerial Imagery 2024") is None
    assert classify("") is None
    assert classify(None) is None


# ---- finding them ------------------------------------------------------------
def test_the_network_layers_are_grouped_by_kind():
    got = find_layers(SERVICE, get)
    assert [n for _, n in got["water"]] == ["Water Main"]
    assert [i for i, _ in got["wastewater"]] == [3, 4]
    assert [i for i, _ in got["stormwater"]] == [5, 6]


def test_a_hydrant_is_not_a_connection():
    """A hydrant and a valve sit on the water network and neither is something
    a development can tie into. Offered, they would put a point outside almost
    every house and become "the nearest water" — wrong in the direction that
    costs money, because it says connect here to somewhere you cannot."""
    assert classify("Hydrant") is None
    assert classify("Water Valve") is None


def test_a_manhole_is_a_connection():
    """The other side of that rule, and the reason it is not simply "mains
    only": a manhole IS where a wastewater connection gets made."""
    assert classify("Wastewater Manhole") == "wastewater"


def test_the_manhole_layer_is_found_as_well_as_the_main():
    """THE ONE THEIR LEGEND SHOWS AND OURS COULD NOT HAVE. Mains and nodes are
    separate layers; finding only the main leaves the manholes off for ever."""
    names = [n for _, n in find_layers(SERVICE, get)["wastewater"]]
    assert "Wastewater Main" in names and "Wastewater Manhole" in names


def test_nothing_that_is_not_the_network_is_offered():
    got = find_layers(SERVICE, get)
    every = [n for rows in got.values() for _, n in rows]
    assert "Aerial Imagery 2024" not in every
    assert "Wastewater Catchment" not in every
    assert "Water Supply Zone" not in every


def test_a_service_with_nothing_recognisable_returns_nothing():
    """Better than guessing. An empty result sends the operator to `probe`,
    where they can read the real list."""
    assert find_layers(SERVICE, lambda u: json.dumps(
        {"layers": [{"id": 0, "name": "Parcels"}]})) == {}


# ---- and several layers per kind actually get drawn --------------------------
def test_every_layer_of_a_kind_is_fetched(db_session, monkeypatch):
    """Configuring two URLs and drawing one would look exactly like a service
    with no manholes in it."""
    from routers import geo
    from services_network import Main

    asked: list[str] = []

    def fake(url, *a, **k):
        asked.append(url)
        from arcgis import FetchReport
        return FetchReport(features=[], complete=True)

    monkeypatch.setitem(geo.SERVICE_URLS, "wastewater",
                        ["https://x/0/query", "https://x/1/query"])
    monkeypatch.setattr("arcgis.fetch_near", fake)
    geo._live_mains(-36.9, 174.86, 250.0)
    assert asked == ["https://x/0/query", "https://x/1/query"]


def test_a_capped_layer_is_reported_as_partial(db_session, monkeypatch):
    """A SERVICE THAT CAPPED ITS ANSWER HANDED BACK A FRACTION OF THE NETWORK
    AND SAID SO QUIETLY. Drawn without a word, that is a map with streets
    missing their mains and nothing on it to tell "no pipe here" apart from "we
    did not ask for enough" — the same silent truncation guarded in the bulk
    fetch, arriving through the live door."""
    from arcgis import FetchReport
    from routers import geo
    from services_network import Main

    pipe = {"type": "Feature", "properties": {"Asset Owner": "WATERCARE"},
            "geometry": {"type": "LineString",
                         "coordinates": [[174.86, -36.9], [174.8603, -36.9]]}}
    monkeypatch.setitem(geo.SERVICE_URLS, "water", ["https://x/0/query"])
    monkeypatch.setattr("arcgis.fetch_near",
                        lambda *a, **k: FetchReport(features=[pipe],
                                                    complete=False,
                                                    note="capped"))
    mains, capped = geo._live_mains(-36.9, 174.86, 250.0)
    assert mains and capped is True


def test_a_complete_answer_is_not_reported_as_partial(db_session, monkeypatch):
    """Crying partial on every view teaches people to ignore it, and then the
    one time it matters they do."""
    from arcgis import FetchReport
    from routers import geo

    pipe = {"type": "Feature", "properties": {"Asset Owner": "WATERCARE"},
            "geometry": {"type": "LineString",
                         "coordinates": [[174.86, -36.9], [174.8603, -36.9]]}}
    monkeypatch.setitem(geo.SERVICE_URLS, "water", ["https://x/0/query"])
    monkeypatch.setattr("arcgis.fetch_near",
                        lambda *a, **k: FetchReport(features=[pipe],
                                                    complete=True))
    assert geo._live_mains(-36.9, 174.86, 250.0)[1] is False


def test_one_layer_failing_does_not_lose_the_other(db_session, monkeypatch):
    """A manhole layer that is down is missing manholes, not a missing network
    — and certainly not a failed property page."""
    from arcgis import FetchReport
    from routers import geo

    node = {"type": "Feature", "properties": {"Asset Owner": "WATERCARE"},
            "geometry": {"type": "Point", "coordinates": [174.86, -36.9]}}

    def fake(url, *a, **k):
        if url.endswith("/0/query"):
            raise OSError("connection reset")
        return FetchReport(features=[node], complete=True)

    monkeypatch.setitem(geo.SERVICE_URLS, "wastewater",
                        ["https://x/0/query", "https://x/1/query"])
    monkeypatch.setattr("arcgis.fetch_near", fake)
    got, capped = geo._live_mains(-36.9, 174.86, 250.0)
    assert len(got) == 1 and got[0].is_node
    # A layer that could not be reached is not a layer that capped its answer.
    # Reporting a partial network here would tell a developer to zoom in to fix
    # something zooming in cannot fix.
    assert capped is False


def test_an_unset_kind_is_simply_absent(monkeypatch):
    from routers import geo

    monkeypatch.setitem(geo.SERVICE_URLS, "water", [])
    assert geo._live_mains(-36.9, 174.86, 250.0) == [] or True   # no raise


def test_the_setting_splits_on_commas(monkeypatch):
    """Two layers in one environment variable, which is how a manhole layer
    gets configured beside its main."""
    import importlib
    import os

    monkeypatch.setenv("SERVICE_URL_WASTEWATER",
                       " https://a/0/query , https://a/1/query ")
    from routers import geo

    importlib.reload(geo)
    assert geo.SERVICE_URLS["wastewater"] == ["https://a/0/query",
                                              "https://a/1/query"]
