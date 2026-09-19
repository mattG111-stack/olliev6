"""What this file will do, before it does it.

    "i need to be able to download the data before we load it"

A load is currently a one-way door. You upload 146 MB, it decides, and what it
decided is a count: 11,773 rejected. The rows are gone — not stored, not
listed, not recoverable — so "why isn't 36 Lloyd Ave in the system" has no
answer at all, and "10,608 not in this region" on a file named
auckland_v2.csv is a fact you can only stare at.

This runs the same decisions and writes down every one, without touching the
database. Upload the file, get a CSV back with one line per row of the original
and what would happen to it:

    Address              Suburb    Verdict   Why
    12 Elliot Street     Remuera   loaded
    36 Lloyd Ave         Mt Albert REJECTED  region reads "auckland", not Auckland
    9 Apartment Way      CBD       REJECTED  apartments are deliberately excluded

Deliberately NOT a summary. A summary is what the run log already gives, and it
was not enough: it can tell you 10,608 rows were refused and it cannot tell you
whether yours was one of them. The row is the unit of the question.
"""
from __future__ import annotations

import csv
import io

import pandas as pd

# The reject rules are ingest's, not a second copy — a preflight that disagrees
# with the real load is worse than none, because it is trusted.
from .ingest import (_EXCLUDED_LOCATIONS, _common_property_payload, _to_float,
                     canonical_type, normalise_columns, region_is_unreadable,
                     region_matches)
# assumptions, not audit. The load aliases this module `A` and calls
# A.is_vacant_type; this file copied the call and aliased the wrong module, so
# the last check raised AttributeError instead of answering — on any row with no
# floor area, which the real file had 107 of. It was invisible because it is the
# one branch reached only when every earlier check has passed.
from .pricing import assumptions as A

WHY = {
    "non_target_region": 'region reads "{v}", not {region}',
    "no_suburb": "no suburb, so nothing to compare it against",
    "excluded_location": "in an area with no comparable sales",
    "apartment_excluded": "apartments are deliberately excluded — their $/m2 is "
                          "too noisy to price",
    "no_cv": "no council valuation, which every valuation method needs",
    "asking_vs_cv_50pct": "asking ${ask:,.0f} is more than 50% away from the "
                          "${cv:,.0f} council valuation — one of the two is wrong",
    "placeholder_asking": "asking under ${floor:,.0f} — a by-negotiation "
                          "placeholder, not a price",
    "empty_row": "no beds, no floor area, no council valuation and no price",
    "dwelling_no_floor": "a building with no floor area, so it cannot be size-valued",
}


def verdict(payload: dict, row: dict, region: str) -> tuple[str | None, dict]:
    """(reason_code, facts) — None means the row would load.

    The order matters and mirrors the load exactly: a row refused for two
    reasons is reported under the first one, which is the one you would fix.
    """
    asking = _to_float(row.get("price_numeric"))
    cv = payload.get("cv_numeric")
    facts = {"v": payload.get("region"), "region": region,
             "ask": asking or 0, "cv": cv or 0,
             "floor": A.ASKING_PRICE_MIN}

    if not region_matches(payload.get("region"), region):
        return "non_target_region", facts
    if not payload.get("suburb"):
        return "no_suburb", facts
    _loc = f"{payload.get('suburb') or ''} {payload.get('address') or ''}".lower()
    if any(x in _loc for x in _EXCLUDED_LOCATIONS):
        return "excluded_location", facts
    if canonical_type(payload.get("property_type")) == "Apartment":
        return "apartment_excluded", facts
    if not cv:
        return "no_cv", facts
    if asking and cv and abs(float(asking) - float(cv)) > 0.50 * float(cv):
        return "asking_vs_cv_50pct", facts
    if asking is not None and asking < A.ASKING_PRICE_MIN:
        return "placeholder_asking", facts
    if (payload.get("beds") is None and payload.get("floor_area_m2") is None
            and payload.get("cv_numeric") is None and asking is None):
        return "empty_row", facts
    if payload.get("floor_area_m2") is None \
            and not A.is_vacant_type(row.get("property_type")):
        return "dwelling_no_floor", facts
    return None, facts


def check(df: pd.DataFrame, region: str = "Auckland") -> tuple[bytes, dict]:
    """(csv_bytes, counts). Reads the frame; writes nothing anywhere."""
    df = normalise_columns(df)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Address", "Suburb", "Region", "Type", "Asking", "CV",
                "Floor", "Land", "Verdict", "Why"])

    counts: dict[str, int] = {}
    loaded = 0
    for _, r in df.iterrows():
        row = r.to_dict()
        payload = _common_property_payload(row, region=region)
        code, facts = verdict(payload, row, region)
        if code is None:
            loaded += 1
            # A row can load and still be worth a word. This one got in because
            # its region string could not be read as any region at all, which is
            # deliberate — but you should be told, not have it pass silently.
            why = ""
            if region_is_unreadable(payload.get("region"), region):
                counts["_unreadable_region"] = counts.get("_unreadable_region", 0) + 1
                why = (f'loaded — region reads "{payload.get("region")}", which is '
                       f"no region we know, so it is not evidence of anywhere else")
        else:
            counts[code] = counts.get(code, 0) + 1
            try:
                why = WHY.get(code, code).format(**facts)
            except Exception:                     # never let wording break a report
                why = WHY.get(code, code)
        w.writerow([
            payload.get("address"), payload.get("suburb"), payload.get("region"),
            payload.get("property_type"),
            _to_float(row.get("price_numeric")) or "",
            payload.get("cv_numeric") or "",
            payload.get("floor_area_m2") or "", payload.get("land_area_m2") or "",
            "loaded" if code is None else "REJECTED", why,
        ])

    counts["_loaded"] = loaded
    counts["_total"] = int(len(df))
    return buf.getvalue().encode("utf-8-sig"), counts
