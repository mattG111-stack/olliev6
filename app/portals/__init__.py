"""The portals we ask about a property, behind one shape.

After the deals are worked out there are a few dozen keepers a week, and for
each one there are public pages that know things we do not: a floor area we are
missing, a land area, a council valuation, and each portal's own estimate of
what the place is worth.

Every source answers in the same shape — a PortalResult — so the runner does not
care which one it is talking to, and adding a fifth is one module rather than a
change everywhere. What a result may do is deliberately narrow:

  fill      a field we are MISSING. Never an overwrite. Our own feed carries
            things no portal has, and a value we already hold is not improved by
            a second opinion about it.
  estimate  that portal's own figure, kept in that portal's own columns and
            shown as theirs. Never an input to our valuation, our margin, our
            buy price or a deal flag — see the note in app/trademe.py for what
            happens when a portal's "estimate" turns out to be the sale price
            handed back.

Sources, and how each is reached:

  corelogic     propertyvalue.co.nz, fetched directly. Already in use.
  homes         homes.co.nz, fetched directly. Its estimate sits in the page
                JSON, so no browser is needed.
  oneroof       via Apify.
  trademe       via Apify.
  realestate    via Apify.

The last three render their figures client-side, which means a headless browser.
Running one inside the API container for thirty lookups a week is the wrong
trade — it is a large dependency, a large memory footprint, and a scraper to
maintain against three sites that change without telling us. Apify runs the
browser, and at this volume it costs cents a week.
"""

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
