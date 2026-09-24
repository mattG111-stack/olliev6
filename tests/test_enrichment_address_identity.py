"""An external search result must identify the requested property."""
import pytest
import propertyvalue as pv


def suggestion(address, suburb="Swanson", identity="42"):
    return {"suggestion": address, "suburbName": suburb, "propertyId": identity}


@pytest.mark.parametrize("wrong", [
    suggestion("4 Monte Cristal Avenue"),
    suggestion("1/6 Monte Cristal Avenue"),
    suggestion("6A Monte Cristal Avenue"),
    suggestion("6 Monte Cristal Avenue", "Henderson"),
    {"propertyId": "42"},
])
def test_wrong_or_unidentified_search_result_is_not_accepted(wrong):
    assert pv._matched_suggestion("6 Monte Cristal Avenue, Swanson, Auckland", [wrong]) is None


def test_correct_second_result_beats_neighbour_ranked_first():
    correct = suggestion("6 Monte Cristal Ave, Swanson")
    assert pv._matched_suggestion("6 Monte Cristal Avenue, Swanson", [
        suggestion("4 Monte Cristal Avenue", identity="41"), correct]) == correct


def test_unit_spaces_and_street_abbreviations_match_without_losing_unit():
    correct = suggestion("3/107 Donovan St", "Blockhouse Bay")
    assert pv._matched_suggestion("3 / 107 Donovan Street, Blockhouse Bay", [correct]) == correct
    assert pv._matched_suggestion("107 Donovan Street, Blockhouse Bay", [correct]) is None


def test_ambiguous_same_address_with_different_ids_is_unresolved():
    assert pv._matched_suggestion("6 Monte Cristal Avenue, Swanson", [
        suggestion("6 Monte Cristal Avenue", identity="1"),
        suggestion("6 Monte Cristal Avenue", identity="2")]) is None


def test_suburb_can_come_from_formatted_address():
    candidate = {"propertyId": "42", "suggestion": "6 Monte Cristal Avenue, Swanson, Auckland"}
    assert pv._matched_suggestion("6 Monte Cristal Ave, Swanson", [candidate]) == candidate


def test_mismatch_never_fetches_or_fills_wrong_property(monkeypatch):
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {"suggestions": [suggestion("4 Monte Cristal Avenue")]}
    class Client:
        def get(self, url, **kwargs):
            calls.append(url)
            assert url.endswith("/suggestions")
            return Response()
    monkeypatch.setattr(pv, "_client", lambda: Client())
    assert pv._lookup_once("6 Monte Cristal Avenue, Swanson") == (None, pv.PV_NOT_FOUND)
    assert len(calls) == 1
