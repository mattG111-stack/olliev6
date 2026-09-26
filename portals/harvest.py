"""Collect configured public search pages and match exact addresses within a suburb. No Apify network calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from trademe import address_key
from portals.direct import CollectorUnavailable, collect

log = logging.getLogger(__name__)

# How many listings to pull per suburb per portal. A suburb rarely has more than
# a couple of hundred on the market; this is a runaway guard, not a target.
PER_SUBURB = 300

# OneRoof is the odd one out: its actor filters by REGION, with no suburb field.
# Its search URLs do carry a suburb, so a suburb harvest goes through startUrls
# and this template. Region is the fallback when a suburb slug is not known.
ONEROOF_SUBURB_URL = "https://www.oneroof.co.nz/search/houses-for-sale/suburb_{slug}"
ONEROOF_REGION = "auckland-35"


def _slug(name: str) -> str:
    return "-".join(str(name or "").strip().lower().split())


@dataclass
class Harvest:
    """One portal's listings for one suburb, indexed by address.

    Built once and reused for every property in that suburb — the whole point of
    inverting the lookup. `found` is kept so a run can report "asked OneRoof for
    Papakura, got 214 listings, matched 3 of our 5" instead of silence.
    """

    source: str
    suburb: str
    by_address: dict[str, dict] = field(default_factory=dict)
    total: int = 0
    error: str | None = None

    def get(self, address: str, suburb: str | None = None) -> dict | None:
        key = address_key(address, suburb or self.suburb)
        if key and key in self.by_address:
            return self.by_address[key]
        # Same street, suburb spelled differently by the portal — fall back to
        # the street alone, but only when it is unambiguous in this suburb.
        street = address_key(address, "")
        if not street:
            return None
        hits = [v for k, v in self.by_address.items()
                if k.split("|")[0] == street.split("|")[0]]
        return hits[0] if len(hits) == 1 else None


def _index(source: str, suburb: str, items: list[dict],
           address_of) -> Harvest:
    h = Harvest(source=source, suburb=suburb, total=len(items))
    for item in items:
        if not isinstance(item, dict):
            continue
        addr = item.get("address") if item.get("_apex_direct") else address_of(item)
        key = address_key(addr, suburb)
        if key:
            h.by_address.setdefault(key, item)
    return h


def harvest(source: str, actor: str, payload: dict, suburb: str,
            address_of, *, limit: int = PER_SUBURB) -> Harvest:
    """Run one actor for one suburb and index the result.

    Never raises. A portal having a bad day costs that portal's fills for that
    suburb and nothing else — the other portals and the other suburbs are
    unaffected, and every property keeps whatever it already had.
    """
    try:
        items = collect(source, suburb=suburb, cap=limit)
    except CollectorUnavailable as e:
        log.info("%s harvest of %s unavailable: %s", source, suburb, e)
        return Harvest(source=source, suburb=suburb, error=str(e))
    except Exception as e:                        # noqa: BLE001
        log.warning("%s harvest of %s failed: %s", source, suburb, e)
        return Harvest(source=source, suburb=suburb, error=f"{type(e).__name__}: {e}")
    return _index(source, suburb, items, address_of)


class HarvestCache:
    """Suburb harvests for the life of one run, per portal.

    Without this, inverting the lookup would be a straight loss: one actor run
    per property instead of one per suburb. With it, a batch of thirty keepers
    spread over six suburbs asks each portal six times.
    """

    def __init__(self) -> None:
        self._by: dict[tuple[str, str], Harvest] = {}

    def get(self, source: str, suburb: str, build) -> Harvest:
        key = (source, _slug(suburb))
        if key not in self._by:
            self._by[key] = build()
            h = self._by[key]
            log.info("%s: %s → %d listings, %d addresses%s",
                     source, suburb, h.total, len(h.by_address),
                     f" ({h.error})" if h.error else "")
        return self._by[key]

    def stats(self) -> list[dict]:
        """What each harvest actually returned — for the run's findings row."""
        return [{"source": h.source, "suburb": h.suburb, "listings": h.total,
                 "addresses": len(h.by_address), "error": h.error}
                for h in self._by.values()]
