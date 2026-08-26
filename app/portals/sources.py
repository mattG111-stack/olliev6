"""One function per portal. Address in, PortalResult out, or nothing.

The two that can be read without a browser are wrapped around code that already
existed and is already in use: give them an address, they resolve that property's
own page and read it. That is what a lookup should look like.

The three that need a browser go through Apify, and they do NOT work that way.
Every New Zealand property actor in the store searches by region, suburb or a URL
you already have; not one takes an address. So those three ask for the SUBURB and
find our address in the answer — see harvest.py for why, and for what the
address-shaped version was silently doing instead.

Every one of them is best-effort by contract: they return None rather than
raising, because the caller is working through a list of properties and one
portal having a bad day must not cost the other twenty-nine their lookups.

The payloads and field names below are checked against each actor's published
input and output schema, not guessed. Where a name here does not appear in the
actor's schema it is a deliberate alias for a sibling actor, and the one the
actor really uses is named in a comment.
"""

from __future__ import annotations

import logging

from . import PortalResult
from .apify import ApifyUnavailable, num, pick, run_actor
from .harvest import ONEROOF_REGION, ONEROOF_SUBURB_URL, HarvestCache, _slug, harvest

log = logging.getLogger(__name__)

# Which Apify actor answers for each portal. Overridable per deployment, because
# actors are published by independent developers and the best one changes.
ACTORS = {
    "oneroof": "solidcode/oneroof-co-nz-scraper",
    "trademe": "parseforge/trade-me-property-scraper",
    "realestate": "fatihtahta/realestate-co-nz-scraper",
}

# Which of them carry the portal's OWN valuation, and which only carry listing
# facts. Worth stating plainly because the code used to claim all three were
# "chosen for returning the portal's own estimate", and for Trade Me that is
# simply not true — its actor returns no valuation field of any kind.
HAS_ESTIMATE = {"oneroof": True, "realestate": True, "trademe": False}


def _query(address: str, suburb: str | None) -> str:
    bits = [str(address or "").strip()]
    if suburb and str(suburb).strip().lower() not in bits[0].lower():
        bits.append(str(suburb).strip())
    return ", ".join(b for b in bits if b)


# ---- the two that need no browser -------------------------------------------
def corelogic(address: str, suburb: str | None = None, **_) -> PortalResult | None:
    """propertyvalue.co.nz. Already used by the enrich stage; same lookup."""
    from ..propertyvalue import pv_lookup

    try:
        pv = pv_lookup(_query(address, suburb))
    except Exception as e:                       # noqa: BLE001
        log.info("corelogic lookup failed: %s", e)
        return None
    if not pv:
        return None
    return PortalResult(
        source="corelogic",
        url=pv.get("url"),
        estimate=pv.get("estimate_mid"),
        estimate_low=pv.get("estimate_low"),
        estimate_high=pv.get("estimate_high"),
        floor_area_m2=pv.get("floor_area_m2"),
        land_area_m2=pv.get("land_area_m2"),
        beds=pv.get("beds"),
        baths=pv.get("baths"),
        cv_numeric=pv.get("cv"),
        land_value_numeric=pv.get("land_value"),
        improvement_value_numeric=pv.get("improvement_value"),
        year_built=pv.get("year_built"),
        property_type=pv.get("property_type"),
        raw=pv,
    )


def homes(address: str, suburb: str | None = None, **_) -> PortalResult | None:
    """homes.co.nz. Its estimate is in the page JSON, so a fetch is enough."""
    from ..external_estimates import homes_estimate

    try:
        est = homes_estimate(_query(address, suburb))
    except Exception as e:                       # noqa: BLE001
        log.info("homes lookup failed: %s", e)
        return None
    if not est:
        return None
    return PortalResult(
        source="homes",
        url=est.get("url"),
        estimate=est.get("value"),
        estimate_low=est.get("low"),
        estimate_high=est.get("high"),
        cv_numeric=est.get("cv"),
        raw=est,
    )


# ---- the three that go through a suburb harvest ------------------------------
def _first_url(v):
    """An image field that is sometimes a URL and sometimes a list of them."""
    if isinstance(v, (list, tuple)):
        for x in v:
            if isinstance(x, str) and x.strip():
                return x
            if isinstance(x, dict):
                got = x.get("url") or x.get("base_url")
                if got:
                    return got
        return None
    return v if isinstance(v, str) and v.strip() else None


def _oneroof_result(item: dict) -> PortalResult:
    """solidcode/oneroof-co-nz-scraper.

    Its own estimate is `estimatedValue`, and the council value is
    `ratingValuation` — NOT capitalValue/cv/rateableValue, which is what the
    first version looked for, so no OneRoof CV was ever collected. It publishes
    no low/high band and no year built; those stay blank rather than being
    invented. Areas and money arrive as strings ("$1,250,000", "210m²") and
    num() is what turns them into numbers.
    """
    return PortalResult(
        source="oneroof",
        url=pick(item, "url"),
        estimate=num(pick(item, "estimatedValue")),
        # No band published by this actor.
        floor_area_m2=num(pick(item, "floorArea")),
        land_area_m2=num(pick(item, "landArea")),
        beds=num(pick(item, "bedrooms")),
        baths=num(pick(item, "bathrooms")),
        cars=num(pick(item, "parking")),
        cv_numeric=num(pick(item, "ratingValuation")),
        land_value_numeric=num(pick(item, "landValue")),
        improvement_value_numeric=num(pick(item, "improvementValue")),
        property_type=pick(item, "propertyType"),
        image_url=_first_url(item.get("images")),
        raw=item,
    )


def _trademe_result(item: dict) -> PortalResult:
    """parseforge/trade-me-property-scraper.

    Facts only. This actor returns no valuation, no CV and no rating split, so
    Trade Me can fill a missing floor area or bed count and nothing else. Its
    areas are already numbers; its image is `pictureHref`.
    """
    return PortalResult(
        source="trademe",
        url=pick(item, "url"),
        floor_area_m2=num(pick(item, "floorArea")),
        land_area_m2=num(pick(item, "landArea")),
        beds=num(pick(item, "bedrooms")),
        baths=num(pick(item, "bathrooms")),
        cars=num(pick(item, "totalParking", "parking")),
        property_type=pick(item, "propertyType"),
        image_url=_first_url(pick(item, "pictureHref", "photoUrls")),
        raw=item,
    )


def _realestate_result(item: dict) -> PortalResult:
    """fatihtahta/realestate-co-nz-scraper.

    Everything useful is nested: property.*, location.*, entity.url, media.photos.
    Its CV and estimate only exist when the run asked for them — see
    `get_valuations` in the payload below, which defaults to false and was not
    being set, so even a perfect address match would have carried no valuation.
    """
    prop = item.get("property") if isinstance(item.get("property"), dict) else {}
    ent = item.get("entity") if isinstance(item.get("entity"), dict) else {}
    media = item.get("media") if isinstance(item.get("media"), dict) else {}
    val = item.get("valuation") if isinstance(item.get("valuation"), dict) else {}

    return PortalResult(
        source="realestate",
        url=ent.get("url") or pick(item, "url", "listing_url"),
        estimate=num(pick(val, "estimated_value", "estimate", "mid")
                     or pick(item, "estimated_value", "estimate")),
        estimate_low=num(pick(val, "estimate_low", "low")),
        estimate_high=num(pick(val, "estimate_high", "high")),
        floor_area_m2=num(prop.get("floor_area")),
        land_area_m2=num(prop.get("land_area")),
        beds=num(prop.get("bedrooms")),
        baths=num(prop.get("bathrooms")),
        cars=num(prop.get("garages") or prop.get("carports")
                 or prop.get("parking_spaces_covered")),
        cv_numeric=num(pick(val, "capital_value", "council_value", "cv")
                       or pick(item, "capital_value")),
        land_value_numeric=num(pick(val, "land_value")),
        improvement_value_numeric=num(pick(val, "improvement_value")),
        property_type=prop.get("property_type"),
        image_url=_first_url((media.get("photos") or [])),
        raw=item,
    )


def _realestate_address(item: dict) -> str | None:
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    return loc.get("full_address") or loc.get("address")


# Per portal: the actor, how to build a suburb payload, how to read an address
# out of one of its items, and how to turn an item into a PortalResult.
_PORTALS = {
    "oneroof": dict(
        # No suburb field on this actor — only `region` and `startUrls`. Its
        # search URLs do carry a suburb, so the suburb goes through startUrls
        # and region is the fallback. `mode` must be one of the actor's enum
        # values; the first version sent "search", which is not one of them, and
        # the run quietly fell back to every listing in the country.
        payload=lambda suburb: {
            "mode": "houses-for-sale",
            "region": ONEROOF_REGION,
            "startUrls": [ONEROOF_SUBURB_URL.format(slug=_slug(suburb))]
                         if suburb else [],
            "includeDetails": True,     # what carries CV and the rating split
            "maxResults": 300,
        },
        address=lambda item: item.get("address"),
        result=_oneroof_result,
    ),
    "trademe": dict(
        # This actor DOES filter by suburb. `listingType` must be
        # "residential-sale" — the first version sent "sale", which is not in
        # the enum, and the actor's schema is strict, so every run was refused.
        payload=lambda suburb: {
            "listingType": "residential-sale",
            "region": "auckland",
            "suburb": _slug(suburb),
            "maxItems": 300,
        },
        address=lambda item: item.get("address"),
        result=_trademe_result,
    ),
    "realestate": dict(
        # Suburbs is an array, the cap is `limit` (not maxItems, which this
        # actor ignores), and get_valuations is what turns on the council CV and
        # the estimate. All three were wrong or missing before.
        payload=lambda suburb: {
            "listing_type": "sale",
            "city": ["Auckland"],
            "suburbs": [str(suburb)] if suburb else [],
            "get_valuations": True,
            "limit": 300,
        },
        address=_realestate_address,
        result=_realestate_result,
    ),
}


def _via_harvest(source: str, address: str, suburb: str | None,
                 cache: HarvestCache | None) -> PortalResult | None:
    if not suburb:
        # Without a suburb there is nothing to harvest and no way to be sure a
        # match is the right house. Saying nothing beats guessing.
        return None
    spec = _PORTALS[source]
    cache = cache if cache is not None else HarvestCache()
    h = cache.get(source, suburb,
                  lambda: harvest(source, ACTORS[source], spec["payload"](suburb),
                                  suburb, spec["address"]))
    item = h.get(address, suburb)
    if not item:
        return None
    res = spec["result"](item)
    return res if res.has_anything() else None


def oneroof(address: str, suburb: str | None = None,
            cache: HarvestCache | None = None) -> PortalResult | None:
    return _via_harvest("oneroof", address, suburb, cache)


def trademe(address: str, suburb: str | None = None,
            cache: HarvestCache | None = None) -> PortalResult | None:
    return _via_harvest("trademe", address, suburb, cache)


def realestate(address: str, suburb: str | None = None,
               cache: HarvestCache | None = None) -> PortalResult | None:
    return _via_harvest("realestate", address, suburb, cache)


LOOKUPS = {
    "corelogic": corelogic,
    "homes": homes,
    "oneroof": oneroof,
    "trademe": trademe,
    "realestate": realestate,
}
