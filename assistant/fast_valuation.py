"""Direct reports for explicit address valuations using existing evidence tools."""
import json
import math
import re
from assistant.providers import Result
from assistant.record_report import cell
from assistant.suburb_brief import load_brief

def direct_valuation(question, dispatch, history=None):
    if history:
        return None
    match = re.fullmatch(r"\s*what is\s+(\d[^,\n?]{2,150}),\s*([A-Za-z][A-Za-z '\-]{1,80}?)\s+worth\?\s*", question, re.I)
    if not match:
        return None
    address, suburb = match.groups()
    try:
        data = json.loads(dispatch("find_address", {"address":address.strip(), "suburb":suburb.strip()}))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("not_in_our_data"):
        return None
    estimate = data.get("apex_estimate") or {}
    rec = data.get("corelogic") or {}
    homes = data.get("homes_co_nz") or {}
    if not estimate.get("available") or not isinstance(estimate.get("value"), (int, float)) or not math.isfinite(estimate["value"]) or estimate["value"] <= 0:
        return None
    if str(estimate.get("suburb","")).casefold() != suburb.strip().casefold():
        return None
    money = lambda value: "$" + format(value,",.0f")
    lines = [f"## {cell(address)}, {cell(suburb)}",
             "**Apex estimate: " + money(estimate["value"]) + "**", "",
             "| Recorded facts | Details |", "|---|---|",
             f"| Bedrooms / bathrooms | {cell(rec.get('beds'))} / {cell(rec.get('baths'))} |",
             f"| Floor / land area (m²) | {cell(rec.get('floor_area_m2'))} / {cell(rec.get('land_area_m2'))} |",
             f"| Zoning | {cell(rec.get('zoning'))} |", "",
             "| Source | Estimate |", "|---|---|"]
    for label, value in [("CoreLogic",rec.get("estimate_mid")),("Homes.co.nz",homes.get("value"))]:
        if isinstance(value,(float,int)) and value>0:
            lines.append("| " + label + " (external opinion) | " + money(value) + " |")
    lines += ["", "**Does this property have a swimming pool?**"]
    used = ["find_address"]
    try:
        lines += ["", load_brief(suburb.strip())]
        used.append("suburb_snapshot")
    except Exception:
        lines += ["", "Suburb statistics are temporarily unavailable."]
    return Result(text="\n".join(lines),tools_used=used)
