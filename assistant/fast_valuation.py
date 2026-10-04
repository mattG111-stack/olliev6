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

def _pool_area_percent(suburb, dispatch):
    from db import SessionLocal
    from models import PropertySold as S
    from ingest import sold_batch_ids
    from sqlalchemy import func
    with SessionLocal() as db:
        districts = {r[0].strip() for r in db.query(S.district).filter(
            S.import_batch_id.in_(sold_batch_ids(db, "Auckland")),
            func.lower(func.trim(S.suburb)) == suburb.casefold()).distinct().all() if r[0]}
    if len(districts) != 1:
        raise ValueError("Ambiguous district")
    district = next(iter(districts))
    rows = json.loads(dispatch("renovation_value_by_district", {}))["districts"]
    matches = [r for r in rows if r["district"].casefold() == district.casefold()]
    if len(matches) != 1:
        raise ValueError("Missing district evidence")
    raw = matches[0].get("pool_gap")
    if raw is None or str(raw).strip() in {"", "—", "N/A"}:
        return None
    value = float(str(raw).replace("%", ""))
    if not math.isfinite(value):
        raise ValueError("Invalid pool evidence")
    return value


def direct_pool_followup(question, dispatch, history):
    """Only answer an immediate pool confirmation after our direct address report."""
    if not history or len(history) < 2:
        return None
    if not re.fullmatch(r"\s*yes(?:,?\s+it has a pool)?[.!]?(?:\s+what is the updated apex estimate[?]?)?\s*", question, re.I):
        return None
    previous, answer = history[-2:]
    if previous.role != "user" or answer.role != "assistant":
        return None
    if "**Does this property have a swimming pool?**" not in answer.content:
        return None
    match = re.fullmatch(r"\s*what is\s+(\d[^,\n?]{2,150}),\s*([A-Za-z][A-Za-z '\-]{1,80}?)\s+worth\?\s*", previous.content, re.I)
    if not match:
        return None
    address, suburb = [v.strip() for v in match.groups()]
    try:
        data = json.loads(dispatch("find_address", {"address":address, "suburb":suburb}))
        estimate = data["apex_estimate"]
        base = estimate["unrounded_value"]
        if not data.get("not_in_our_data") or not estimate.get("available") or estimate.get("suburb","").casefold() != suburb.casefold():
            return None
        if not isinstance(base,(int,float)) or not math.isfinite(base) or base <= 0:
            return None
        area = _pool_area_percent(suburb, dispatch)
        adjusted = json.loads(dispatch("pool_policy_adjustment", {
            "base_value":base, "base_source":"Apex address estimate", "has_pool":True,
            "base_includes_pool":"unknown", "area_pool_percent":area}))
        value = adjusted["value"]
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value <= 0:
            return None
    except (KeyError, TypeError, ValueError, RuntimeError):
        return None
    return Result(text=f"## {cell(address)}, {cell(suburb)}\n\n**Updated Apex estimate: $" + format(value,",.0f") + "**\n\nSwimming pool: confirmed by you. Includes an assumed pool adjustment; whether the base estimate already includes it is unverified.",
                  tools_used=["find_address","renovation_value_by_district","pool_policy_adjustment"])
