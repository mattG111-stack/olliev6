"""Ask for a suburb, find our address in it — and never answer with a stranger.

The three browser portals were called as `lookup(address)`. Checked against the
actors' published input schemas, none of them can do that, and not one New
Zealand property actor in the Apify store can: they search by region, suburb or
a URL you already have.

That was worse than a lookup that fails. OneRoof was sent `mode="search"` (not a
value in its enum) and a `query` field it has never heard of, so the run fell
back to its defaults — every listing in the country — and the first three came
back. Those would have been written onto our property as its floor area, its
land area, its council value. A different house entirely, arriving quietly and
looking completely ordinary.

These tests pin the inversion, the address matching, and the field names that
were wrong. They use recorded actor output rather than a network: the shapes
below are taken from each actor's published output schema.
"""
from __future__ import annotations

from app.portals import harvest as H
from app.portals.harvest import Harvest, HarvestCache
from app.portals.sources import (
    ACTORS,
    HAS_ESTIMATE,
    _PORTALS,
    _oneroof_result,
    _realestate_result,
    _trademe_result,
)

# ---------------------------------------------------------------------------
# Recorded shapes, per each actor's published output schema
# ---------------------------------------------------------------------------
ONEROOF_ITEM = {
    "address": "12 Cassino Terrace, Papakura",
    "estimatedValue": "$1,250,000",
    "ratingValuation": "$1,100,000",      # NOT capitalValue — the bug
    "landValue": "$700,000",
    "improvementValue": "$400,000",
    "floorArea": "182m²",
    "landArea": "612m²",
    "bedrooms": 4, "bathrooms": 2, "parking": 2,
    "propertyType": "House",
    "images": ["https://img.oneroof.co.nz/a.jpg", "https://img.oneroof.co.nz/b.jpg"],
    "url": "https://www.oneroof.co.nz/property/12-cassino-terrace",
}

TRADEME_ITEM = {
    "address": "12 Cassino Terrace, Papakura",
    "floorArea": 182, "landArea": 612,
    "bedrooms": 4, "bathrooms": 2, "totalParking": 2,
    "propertyType": "House",
    "pictureHref": "https://trademe.tmcdn.co.nz/photo.jpg",
    "startPrice": 1_150_000, "priceDisplay": "Enquiries over $1,150,000",
    "url": "https://www.trademe.co.nz/a/property/residential/sale/12345",
}

REALESTATE_ITEM = {
    "entity": {"url": "https://www.realestate.co.nz/1234567", "title": "Family home"},
    "location": {"full_address": "12 Cassino Terrace, Papakura",
                 "suburb": "Papakura", "address": "12 Cassino Terrace"},
    "property": {"bedrooms": 4, "bathrooms": 2, "floor_area": 182.0,
                 "land_area": 612.0, "property_type": "House", "garages": 2,
                 "pool": False},
    "media": {"photos": [{"order": 0, "base_url": "https://cdn.realestate.co.nz/p.jpg"}]},
    "pricing": {"price_text": "Enquiries over $1,150,000"},
}


# ---------------------------------------------------------------------------
# The field names that were wrong
# ---------------------------------------------------------------------------
def test_oneroof_council_value_is_ratingvaluation():
    """The old mapping looked for capitalValue / cv / rateableValue / rv. This
    actor calls it `ratingValuation`, so no OneRoof CV was ever collected."""
    r = _oneroof_result(ONEROOF_ITEM)
    assert r.cv_numeric == 1_100_000
    assert r.land_value_numeric == 700_000
    assert r.improvement_value_numeric == 400_000


def test_oneroof_reads_its_estimate_and_areas_out_of_strings():
    """This actor returns money and areas as text, not numbers."""
    r = _oneroof_result(ONEROOF_ITEM)
    assert r.estimate == 1_250_000
    assert r.floor_area_m2 == 182
    assert r.land_area_m2 == 612
    assert (r.beds, r.baths, r.cars) == (4, 2, 2)


def test_oneroof_takes_the_first_of_its_image_list():
    """`images` is an array; the old mapping looked for a single `imageUrl`."""
    assert _oneroof_result(ONEROOF_ITEM).image_url.endswith("a.jpg")


def test_oneroof_publishes_no_band_so_we_invent_none():
    r = _oneroof_result(ONEROOF_ITEM)
    assert r.estimate_low is None and r.estimate_high is None


def test_trademe_carries_facts_and_no_valuation():
    """Its actor returns no estimate, no CV and no rating split. The code used
    to claim all three portals were picked for their own estimate; for Trade Me
    that was never true, and an asking price must not stand in for one."""
    r = _trademe_result(TRADEME_ITEM)
    assert r.floor_area_m2 == 182 and r.land_area_m2 == 612
    assert (r.beds, r.baths, r.cars) == (4, 2, 2)
    assert r.estimate is None and r.cv_numeric is None
    assert HAS_ESTIMATE["trademe"] is False
    # The listing price is right there in the item and must not become a value.
    assert r.estimate != 1_150_000


def test_realestate_reads_its_nested_shape():
    """Everything useful is under property / location / entity / media."""
    r = _realestate_result(REALESTATE_ITEM)
    assert r.floor_area_m2 == 182 and r.land_area_m2 == 612
    assert (r.beds, r.baths) == (4, 2)
    assert r.property_type == "House"
    assert r.url == "https://www.realestate.co.nz/1234567"
    assert r.image_url.endswith("p.jpg")


# ---------------------------------------------------------------------------
# The payloads the actors actually accept
# ---------------------------------------------------------------------------
def test_oneroof_mode_is_one_the_actor_recognises():
    """"search" is not in this actor's enum. Sending it is what made the run
    fall back to every listing in New Zealand."""
    p = _PORTALS["oneroof"]["payload"]("Papakura")
    assert p["mode"] in ("houses-for-sale", "houses-for-rent", "sold", "rural")
    assert "query" not in p, "this actor has no query field; it is not a search"
    assert p["includeDetails"] is True, "CV and the rating split need this on"
    assert "papakura" in p["startUrls"][0]


def test_trademe_listing_type_is_the_enum_value():
    """"sale" is not in the enum; the schema is strict, so every run was refused."""
    p = _PORTALS["trademe"]["payload"]("Papakura")
    assert p["listingType"] == "residential-sale"
    assert p["suburb"] == "papakura"


def test_realestate_asks_for_the_valuations_and_uses_the_right_cap():
    """get_valuations defaults to false — without it there is no CV and no
    estimate even on a perfect match. And the cap is `limit`, not `maxItems`."""
    p = _PORTALS["realestate"]["payload"]("Papakura")
    assert p["get_valuations"] is True
    assert p["listing_type"] == "sale"
    assert p["suburbs"] == ["Papakura"]
    assert "limit" in p and "maxItems" not in p


def test_every_portal_names_a_real_actor():
    for name, actor in ACTORS.items():
        assert "/" in actor, f"{name}: {actor} is not a store name"
        assert name in _PORTALS


# ---------------------------------------------------------------------------
# Matching our address inside the harvest
# ---------------------------------------------------------------------------
def _harvest(items, source="oneroof", suburb="Papakura"):
    return H._index(source, suburb, items, lambda i: i.get("address"))


def test_our_address_is_found_in_the_suburb():
    h = _harvest([ONEROOF_ITEM])
    assert h.get("12 Cassino Terrace, Papakura, Auckland", "Papakura") is not None


def test_a_unit_number_written_either_way_is_one_address():
    """"3 / 107 Donovan St" and "3/107 Donovan Street" are the same house."""
    h = _harvest([{"address": "3/107 Donovan Street, Blockhouse Bay"}],
                 suburb="Blockhouse Bay")
    assert h.get("3 / 107 Donovan St, Blockhouse Bay, Auckland City",
                 "Blockhouse Bay") is not None


def test_an_address_not_in_the_suburb_returns_nothing():
    """The whole point. The old code answered this case with another house."""
    h = _harvest([ONEROOF_ITEM])
    assert h.get("99 Nowhere Road, Papakura", "Papakura") is None


def test_two_same_street_matches_are_refused_rather_than_guessed():
    """A street that appears twice under different suburb spellings is
    ambiguous, and an ambiguous match on the wrong house is the failure this
    whole rewrite exists to stop."""
    h = Harvest(source="oneroof", suburb="Papakura")
    h.by_address = {"12 queen street|papakura": {"a": 1},
                    "12 queen street|papakura central": {"b": 2}}
    assert h.get("12 Queen Street", "Nowhere") is None


def test_a_portal_that_fails_costs_only_that_suburb():
    def boom(*_a, **_k):
        raise RuntimeError("actor on fire")

    h = H.harvest("oneroof", "x/y", {}, "Papakura", boom)
    assert h.by_address == {} and h.error
    assert h.get("12 Cassino Terrace", "Papakura") is None


# ---------------------------------------------------------------------------
# One actor run per suburb, not per property
# ---------------------------------------------------------------------------
def test_a_suburb_is_harvested_once_however_many_properties_want_it():
    """Inverting the lookup only pays if the harvest is shared. Six properties
    in one suburb must cost one actor run, not six."""
    cache = HarvestCache()
    runs = []

    def build():
        runs.append(1)
        return _harvest([ONEROOF_ITEM])

    for _ in range(6):
        cache.get("oneroof", "Papakura", build)
    assert len(runs) == 1


def test_different_suburbs_are_harvested_separately():
    cache = HarvestCache()
    runs = []

    def build():
        runs.append(1)
        return _harvest([ONEROOF_ITEM])

    cache.get("oneroof", "Papakura", build)
    cache.get("oneroof", "Blockhouse Bay", build)
    cache.get("oneroof", "papakura", build)          # same suburb, different case
    assert len(runs) == 2


def test_the_run_can_report_what_each_harvest_returned():
    """"asked OneRoof for Papakura, got 1 listing" beats silence when a portal
    stops answering and nobody notices for a fortnight."""
    cache = HarvestCache()
    cache.get("oneroof", "Papakura", lambda: _harvest([ONEROOF_ITEM]))
    s = cache.stats()[0]
    assert s["source"] == "oneroof" and s["suburb"] == "Papakura"
    assert s["listings"] == 1 and s["addresses"] == 1


def test_a_property_with_no_suburb_is_not_guessed_at():
    """No suburb means no harvest to search and no way to be sure a match is
    the right house. Saying nothing is the honest answer."""
    from app.portals.sources import oneroof

    assert oneroof("12 Cassino Terrace", None) is None


# ---------------------------------------------------------------------------
# Reading a number out of an actor's text
# ---------------------------------------------------------------------------
def test_a_floor_area_with_a_unit_is_not_multiplied_by_ten():
    """The one that would have done real damage.

    num() used to delete every non-digit and parse what was left, so "210 m2"
    became "2102" — a 210 m² house filed as 2,102 m². Floor area sets the $/m²
    comp rate, so that is not a wrong tile, it is a wrong valuation.
    """
    from app.portals.apify import num

    assert num("210 m2") == 210
    assert num("210m2") == 210
    assert num("182 sqm") == 182


def test_a_superscript_area_is_read_not_dropped():
    """"182m²" parsed to "182²" and raised, because Python says the superscript
    two is a digit. A real floor area came back as missing."""
    from app.portals.apify import num

    assert num("182m²") == 182
    assert num("612m²") == 612


def test_a_millions_shorthand_still_means_millions():
    """The multiplier has to survive the fix — "$1.25m" is not $1.25."""
    from app.portals.apify import num

    assert num("$1.25m") == 1_250_000
    assert num("1.2M") == 1_200_000
    assert num("$820K") == 820_000
    assert num("$1,250,000") == 1_250_000


def test_hectares_become_square_metres():
    """Rural land arrives in hectares; everything downstream is m²."""
    from app.portals.apify import num

    assert num("1.2 ha") == 12_000


def test_nothing_useful_is_nothing():
    from app.portals.apify import num

    for v in ("", "  ", None, "POA", "by negotiation", True, False):
        assert num(v) is None


# ---------------------------------------------------------------------------
# Street abbreviations
# ---------------------------------------------------------------------------
def test_an_abbreviated_street_type_matches_the_written_out_one():
    """Our export writes "Donovan Street"; portals write "Donovan St". As bare
    text those were two different houses, so every such address missed."""
    from app.trademe import address_key as k

    assert k("3/107 Donovan Street", "Blockhouse Bay") == \
        k("3 / 107 Donovan St, Blockhouse Bay, Auckland City", "Blockhouse Bay")
    assert k("12 Cassino Terrace", "Papakura") == k("12 Cassino Tce", "Papakura")
    assert k("5 Queen Rd", "Otahuhu") == k("5 Queen Road", "Otahuhu")


def test_only_the_last_word_is_treated_as_a_street_type():
    """"St Heliers Road" begins with a saint, not a street. Rewriting a leading
    abbreviation would invent an address instead of normalising one."""
    from app.trademe import address_key as k

    assert k("1 St Heliers Road", "St Heliers") == k("1 St Heliers Rd", "St Heliers")
    assert "st heliers road" in k("1 St Heliers Road", "St Heliers")
    assert k("9 Mount Albert Road", "Mt Albert") == k("9 Mount Albert Rd", "Mt Albert")


def test_two_different_streets_still_do_not_match():
    from app.trademe import address_key as k

    assert k("12 Queen Street", "Papakura") != k("12 King Street", "Papakura")
    assert k("12 Queen Street", "Papakura") != k("14 Queen Street", "Papakura")
