"""Portal evidence contracts. Missing facts may fill blanks after review; each external valuation stays separate from Apex pricing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PortalResult:
    """What one portal knows about one address."""

    source: str                       # "trademe" | "oneroof" | ...
    url: str | None = None

    # Their own figure. Displayed as theirs, never used as an input.
    estimate: float | None = None
    estimate_low: float | None = None
    estimate_high: float | None = None

    # Facts about the property, used only to fill a field we are missing.
    floor_area_m2: float | None = None
    land_area_m2: float | None = None
    beds: float | None = None
    baths: float | None = None
    cars: float | None = None
    cv_numeric: float | None = None
    land_value_numeric: float | None = None
    improvement_value_numeric: float | None = None
    year_built: float | None = None
    property_type: str | None = None
    image_url: str | None = None

    # Anything the source returned that we do not map, kept for diagnosis.
    raw: dict = field(default_factory=dict)

    def has_anything(self) -> bool:
        return any(getattr(self, f) is not None for f in (
            "estimate", "floor_area_m2", "land_area_m2", "beds", "baths", "cars",
            "cv_numeric", "land_value_numeric", "improvement_value_numeric",
            "year_built", "property_type", "image_url"))


# Which model column each portal's estimate is stored in. Every portal keeps its
# own, so the property page can show them side by side and a reader can see
# where they disagree.
ESTIMATE_COLUMNS: dict[str, tuple[str, str, str, str | None]] = {
    # source:      (mid,                       low,                         high,                        url)
    "homes":       ("homes_valuation",         "homes_valuation_low",       "homes_valuation_high",      "homes_url"),
    "realestate":  ("realestate_valuation",    "realestate_valuation_low",  "realestate_valuation_high", "realestate_url"),
    "trademe":     ("tm_valuation",            "tm_valuation_low",          "tm_valuation_high",         None),
    # OneRoof's OWN columns, since 9.99. It used to be pointed at
    # third_party_valuation — the Hougarden figure that comes with the weekly
    # feed — with a comment claiming it would only fill a blank. Nothing
    # enforced that: fill.py REFRESHES an estimate unconditionally, on purpose,
    # because a portal's estimate moves with their index. So every enrich run
    # overwrote Hougarden's number with OneRoof's, permanently, and the column
    # stopped meaning what its name says.
    "oneroof":     ("oneroof_valuation",       "oneroof_valuation_low",     "oneroof_valuation_high",    "oneroof_url"),
    "corelogic":   ("pv_estimate_mid",         "pv_estimate_low",           "pv_estimate_high",          "pv_url"),
}

# The order they are asked in. Cheapest and most reliable first, so a property
# that is answered early costs less; every source that answers still gets stored.
DEFAULT_ORDER = ("corelogic", "homes", "oneroof", "trademe", "realestate")
