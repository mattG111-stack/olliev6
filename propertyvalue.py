"""On-demand property lookup from propertyvalue.co.nz (CoreLogic NZ).

Unlike homes.co.nz (estimate embedded in page JSON), propertyvalue.co.nz is a
React SPA backed by a public JSON API. Two steps, no login required:

  1) GET /api/public/clapi/suggestions?q=<address>&suggestionTypes=address
       → resolves a free-text address to a canonical address + propertyId
  2) GET /api/public/clapi/properties/<propertyId>
       → full public record: attributes, council rating valuation (CV/LV/IV),
         CoreLogic AVM range, zoning, last sale, market trends

This is CoreLogic's commercial data, so use it the way the site does — a single
on-demand lookup per property, cached, politely rate-limited — NOT a bulk mirror
of the whole database. It can rate-limit or block abusive use, and their terms
apply. Every call degrades to None on any error; it never raises to the caller.

Used for two things:
  - data verification: cross-check our beds/baths/land/floor/zoning/CV against
    an independent authority (see verify.py)
  - a third-party estimate tile on the property page (their AVM alongside ours)
"""
from __future__ import annotations

import logging

import time

import httpx

log = logging.getLogger("ollie.propertyvalue")

_BASE = "https://www.propertyvalue.co.nz"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_HEADERS = {"User-Agent": _UA, "Accept": "application/json", "Referer": f"{_BASE}/"}


def _num(v) -> float | None:
    """CoreLogic returns money as strings ('3075000') or numbers; normalise."""
    if v in (None, "", 0, "0"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _resolve_property_id(address: str, client: httpx.Client) -> dict | None:
    """Address → the top matching {propertyId, suggestion, url, suburb, ...}."""
    r = client.get(
        f"{_BASE}/api/public/clapi/suggestions",
        params={"q": address, "suggestionTypes": "address", "limit": 1},
    )
    if r.status_code != 200:
        return None
    sugg = (r.json() or {}).get("suggestions") or []
    return sugg[0] if sugg else None


def _shape(prop: dict, sugg: dict) -> dict:
    """Flatten the CoreLogic property record into the fields we care about."""
    core = prop.get("core") or {}
    add = prop.get("additional") or {}
    rv = prop.get("ratingValuation") or {}
    er = prop.get("estimatedRange") or {}
    site = prop.get("site") or {}
    loc = prop.get("location") or {}
    sale = (prop.get("sales") or {}).get("lastSale") or {}

    low = _num(er.get("lowerBand"))
    high = _num(er.get("upperBand"))
    mid = round((low + high) / 2) if (low and high) else None

    return {
        "source": "propertyvalue.co.nz",
        "property_id": sugg.get("propertyId"),
        "canonical_address": sugg.get("suggestion") or loc.get("locallyFormattedAddress"),
        "suburb": sugg.get("suburbName"),
        "postcode": sugg.get("postCode") or loc.get("postcode"),
        "url": f"{_BASE}{sugg.get('url')}" if sugg.get("url") else None,
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        # attributes (for verification cross-check)
        "property_type": core.get("propertyType"),
        "property_sub_type": core.get("propertySubType"),
        "beds": core.get("beds"),
        "baths": core.get("baths"),
        "car_spaces": core.get("carSpaces"),
        "garages": core.get("lockUpGarages"),
        "land_area_m2": _num(core.get("landArea")),
        "land_area_is_calculated": core.get("isCalculatedLandArea"),
        "floor_area_m2": _num(add.get("floorArea")),
        "year_built": add.get("yearBuilt"),
        "wall_material": add.get("wallMaterial"),
        "roof_material": add.get("roofMaterial"),
        # zoning (independent check on our zoning corrections)
        "zoning": site.get("zoneDescriptionLocal"),
        "zoning_code": site.get("zoneCodeLocal"),
        # council rating valuation
        "cv": _num(rv.get("capitalValue")),
        "land_value": _num(rv.get("landValue")),
        "improvement_value": _num(rv.get("improvementValue")),
        "rv_date": rv.get("valuationDate"),
        "legal_description": (rv.get("legalDescriptions") or [None])[0] if isinstance(rv.get("legalDescriptions"), list) else rv.get("legalDescriptions"),
        # CoreLogic AVM
        "estimate_low": low,
        "estimate_high": high,
        "estimate_mid": mid,
        "estimate_confidence": er.get("confidence"),
        "estimate_date": er.get("valuationDate"),
        # last sale
        "last_sale_price": _num(sale.get("price")),
        "last_sale_date": sale.get("contractDate"),
        "last_sale_classification": sale.get("saleClassification"),
    }


def _zone_agrees(ours: str | None, theirs: str | None) -> bool:
    """Loose zoning match — the strings are worded differently across sources, so
    compare on the meaningful keyword (residential/rural/business/mixed/etc.)."""
    if not ours or not theirs:
        return True                               # can't compare → don't flag
    a, b = ours.lower(), theirs.lower()
    for kw in ("rural", "business", "industrial", "mixed", "terrace", "apartment",
               "single house", "residential", "town centre", "countryside", "future urban"):
        if kw in a and kw in b:
            return True
        if (kw in a) != (kw in b) and kw in ("rural", "business", "industrial"):
            return False                          # a real category disagreement
    return True


def cross_check(ours: dict, pv: dict) -> list[dict]:
    """Compare our stored fields against the CoreLogic record. Returns a list of
    discrepancies: {field, ours, theirs, severity}. `ours` keys: land_area_m2,
    floor_area_m2, beds, baths, cv, zoning. Empty list = everything agrees."""
    flags: list[dict] = []

    def num(field, o, t, tol_pct, tol_abs, severity):
        # A blank/zero on EITHER side isn't a disagreement — it's missing data
        # (handled as a gap, not a discrepancy). Only flag two real values.
        if not o or not t:
            return
        if abs(o - t) > tol_abs and abs(o - t) / t > tol_pct:
            flags.append({"field": field, "ours": o, "theirs": t, "severity": severity})

    # Land area is the highest-value check — this is what catches bad-data rows
    # (e.g. a listing scraped as 5665 m² that is really 416 m²).
    num("land_area_m2", ours.get("land_area_m2"), pv.get("land_area_m2"), 0.10, 20, "high")
    num("floor_area_m2", ours.get("floor_area_m2"), pv.get("floor_area_m2"), 0.15, 10, "medium")
    # Our CV should equal council CV (CoreLogic sources the same rating value).
    num("cv", ours.get("cv"), pv.get("cv"), 0.05, 1000, "high")

    # NOTE: beds/baths are deliberately NOT checked. CoreLogic sources them from
    # the council record, which lags renovations — a listing showing more beds
    # than CoreLogic is almost always a real improvement, not a data error. The
    # live listing is the more current source, so flagging it is false-positive
    # noise (and wrongly implies our data is the suspect one).

    if not _zone_agrees(ours.get("zoning"), pv.get("zoning")):
        flags.append({"field": "zoning", "ours": ours.get("zoning"),
                      "theirs": pv.get("zoning"), "severity": "high"})
    return flags


# Fields we track that CoreLogic can fill when ours is blank. (last-sale and
# year-built aren't in our schema, so they'd always read as "gaps" — they're
# surfaced separately, not here.)
_GAP_FIELDS = {
    "zoning": "zoning", "land_area_m2": "land_area_m2", "floor_area_m2": "floor_area_m2",
    "beds": "beds", "baths": "baths", "cv": "cv",
}


def gaps(ours: dict, pv: dict) -> list[dict]:
    """Fields CoreLogic has a value for where ours is missing — 'they have, we
    don't'. Returns [{field, theirs}]. `ours` uses the same keys as cross_check
    plus year_built / last_sale_price."""
    out = []
    for key, label in _GAP_FIELDS.items():
        theirs = pv.get(key)
        if theirs in (None, "", 0):
            continue
        if ours.get(key) in (None, "", 0):
            out.append({"field": label, "theirs": theirs})
    return out


# Our field name ← CoreLogic field name, for filling our blanks from their data.
_FILL_PAIRS = (
    ("floor_area_m2", "floor_area_m2"),
    ("land_area_m2", "land_area_m2"),
    ("beds", "beds"),
    ("baths", "baths"),
    ("cv_numeric", "cv"),
    ("zoning", "zoning"),
)


def missing_fills(ours: dict, pv: dict) -> dict:
    """Values to copy from CoreLogic into OUR record where ours is blank or zero.
    Keyed by our field names. Never overwrites a real value — fills gaps only."""
    out = {}
    for our_key, pv_key in _FILL_PAIRS:
        if not ours.get(our_key) and pv.get(pv_key):
            out[our_key] = pv.get(pv_key)
    return out


# Lookup outcomes, so a caller can tell a genuine "CoreLogic has no record for
# this address" (normal, some addresses just aren't in their DB) from being
# actively blocked or rate-limited (403/429) or a transport failure. The enrich
# job uses these to report DEFINITIVELY whether CoreLogic is refusing us, instead
# of guessing from a run of empty results.
PV_OK = "ok"
PV_NOT_FOUND = "not_found"   # resolved cleanly, no property record — a real miss
PV_BLOCKED = "blocked"       # HTTP 403 / 429 — CoreLogic is refusing our requests
PV_ERROR = "error"           # timeout / connection / proxy / parse failure

_BLOCK_CODES = (401, 403, 429)


# ONE connection, reused for the whole run.
#
# This used to open a brand-new client per address, which means a fresh TCP
# connection and a full TLS handshake for every one — 2,141 handshakes on a
# single load, against a host that has every reason to start refusing them. On
# the last run 37% of lookups failed before arriving, with ZERO blocks: not rate
# limiting, transport. Connection churn on that scale is the first thing to
# suspect and the cheapest thing to remove.
#
# httpx.Client is safe to share across threads, and the enrich worker is the
# only caller that runs at volume. Created lazily so importing this module never
# opens a socket, and exposed through a function so tests can still replace it.
_CLIENT: "httpx.Client | None" = None

# A connect that hasn't happened in 10s is not going to; a read can legitimately
# take longer. Split so a slow response isn't mistaken for an unreachable host.
_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
_LIMITS = httpx.Limits(max_connections=4, max_keepalive_connections=4,
                       keepalive_expiry=60.0)

# How many times to re-try a lookup that never arrived. A 401/403/429 already has
# a five-step backoff ladder in the enrich stage; a dropped connection had
# nothing at all, which is backwards — a dropped connection is the single most
# likely thing to succeed on a second attempt.
_RETRIES = 2
_RETRY_WAIT = 1.5


def _client() -> "httpx.Client":
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(headers=_HEADERS, timeout=_TIMEOUT,
                               limits=_LIMITS, follow_redirects=True)
    return _CLIENT


def close_client() -> None:
    """Drop the pooled connection — used when a run finishes, and to recover from
    a connection pool that has gone bad rather than carrying it for hours."""
    global _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        except Exception:
            pass
        _CLIENT = None


def _lookup_once(address: str) -> tuple[dict | None, str]:
    c = _client()
    rs = c.get(f"{_BASE}/api/public/clapi/suggestions",
               params={"q": address, "suggestionTypes": "address", "limit": 1})
    if rs.status_code in _BLOCK_CODES:
        return None, PV_BLOCKED
    if rs.status_code != 200:
        return None, PV_ERROR
    sugg = ((rs.json() or {}).get("suggestions") or [None])[0]
    if not sugg or not sugg.get("propertyId"):
        return None, PV_NOT_FOUND
    r = c.get(f"{_BASE}/api/public/clapi/properties/{sugg['propertyId']}")
    if r.status_code in _BLOCK_CODES:
        return None, PV_BLOCKED
    if r.status_code != 200:
        return None, PV_ERROR
    return _shape(r.json() or {}, sugg), PV_OK


def pv_lookup_status(address: str, timeout: float = 12.0) -> tuple[dict | None, str]:
    """Look up a property, returning (record_or_None, status) where status is one
    of PV_OK / PV_NOT_FOUND / PV_BLOCKED / PV_ERROR. PV_BLOCKED specifically means
    CoreLogic returned 401/403/429 — you're blocked or rate-limited — as opposed to
    the address simply not being in their database (PV_NOT_FOUND).

    A request that never arrives is retried before it is believed. `timeout` is
    accepted for callers that still pass it; the pooled client's split
    connect/read timeouts are used instead.
    """
    if not address or not address.strip():
        return None, PV_NOT_FOUND

    last = "unknown"
    for attempt in range(_RETRIES + 1):
        try:
            rec, status = _lookup_once(address)
            if status != PV_ERROR:
                return rec, status
            last = f"HTTP status not usable (attempt {attempt + 1})"
        except Exception as e:                    # network / proxy / TLS / parse
            last = f"{type(e).__name__}: {e}"
            # A pool that has gone bad stays bad for every later address, so the
            # last attempt drops it and the next lookup reconnects clean.
            if attempt == _RETRIES:
                close_client()
        if attempt < _RETRIES:
            time.sleep(_RETRY_WAIT * (attempt + 1))

    log.info("pv_lookup gave up on %r after %d attempts: %s",
             address, _RETRIES + 1, last)
    return None, PV_ERROR


def pv_lookup(address: str, timeout: float = 12.0) -> dict | None:
    """Look up a single property by address. Returns the flattened record, or
    None if the address can't be resolved or anything fails. (Thin wrapper over
    pv_lookup_status for callers that don't need the reason.)"""
    rec, _status = pv_lookup_status(address, timeout)
    return rec
