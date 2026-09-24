"""An explicit investigation action reports database evidence, not generated advice."""
import html
import json
import math
from decimal import Decimal


def cell(value):
    if value is None or value == "":
        return "Not recorded"
    text = " ".join(str(value).split())[:240]
    text = html.escape(text)
    for char in "\\`*_{}[]()#|!":
        text = text.replace(char, "\\" + char)
    return text


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value > 0 else None


def money(value):
    value = number(value)
    if value is None:
        return "Not recorded"
    return "$" + format(Decimal(str(value)), ",f").rstrip("0").rstrip(".") if "." in str(value) else f"${value:,}"


def object_result(raw):
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def record_report(property_id, dispatch, on_step=None):
    from assistant.providers import Result, _note
    tools = []
    def read(name):
        _note(on_step, "tool", name)
        tools.append(name)
        try:
            return object_result(dispatch(name, {"property_id": property_id}))
        except Exception:
            # Do not expose database errors or turn failures into empty evidence.
            return {}

    record = read("get_property")
    if (type(record.get("id")) is not int or record["id"] != property_id
            or record.get("apex_url") != f"/property/{property_id}"):
        return Result(text="I couldn't verify the selected property record. Please retry before relying on an investigation.", tools_used=tools)
    sold = read("get_sold_comparables")
    comparison = record.get("pricing_comparison")
    comparison = comparison if isinstance(comparison, dict) else {}
    ask = number(comparison.get("asking_price"))
    value = number(comparison.get("apex_value"))
    lines = [f"### {cell(record.get('address'))}",
        "Current Apex record checked. Recorded facts and model estimates are separated below; this is not a valuation or a profit forecast.",
        "", "| Recorded facts | Current record |", "|---|---|",
        f"| Suburb | {cell(record.get('suburb'))} |",
        f"| Asking price (NZD) | {money(ask)} |",
        f"| Sale method | {cell(record.get('sale_method'))} |",
        f"| Bedrooms / bathrooms | {cell(record.get('beds'))} / {cell(record.get('baths'))} |",
        f"| Land / floor area (m²) | {cell(record.get('land_m2'))} / {cell(record.get('floor_m2'))} |",
        f"| Title / zoning | {cell(record.get('title'))} / {cell(record.get('zoning'))} |",
        "", "| Model estimates | Estimate |", "|---|---|",
        f"| Apex estimated value (NZD) | {money(value)} |",
        f"| Model confidence label | {cell(record.get('confidence'))} |",
        f"| Valuation sample count | {cell(record.get('comps_used'))} |"]
    if ask and value:
        gap = Decimal(str(value)) - Decimal(str(ask))
        label = "below" if gap >= 0 else "above"
        lines.append(f"\nAsking is {money(float(abs(gap))) if gap else '$0'} {label} the model estimate. This difference excludes costs and is not an expected return.")
    if record.get("deal_block_reason"):
        lines.append(f"\n**Record warning:** {cell(record['deal_block_reason'])}")
    comps = sold.get("comparables")
    if isinstance(comps, list):
        rows = [row for row in comps if isinstance(row, dict)]
        shown = rows[:3]
        lines += ["", "### Sold evidence"]
        if shown:
            lines += [f"Showing {len(shown)} of {len(rows)} returned sold examples. These are not necessarily the valuation sample or equally comparable homes.",
                "", "| Sold property | Price (NZD) | Sold date | Beds | Land / floor m² |", "|---|---|---|---|---|"]
            for row in shown:
                lines.append(f"| {cell(row.get('address'))} | {money(row.get('sale_price'))} | {cell(row.get('sold_date'))} | {cell(row.get('beds'))} | {cell(row.get('land_m2'))} / {cell(row.get('floor_m2'))} |")
        else:
            lines.append("No sold examples were returned. There is no sold evidence here to substantiate the estimate.")
    else:
        lines += ["", "**Sold evidence unavailable:** the lookup did not return a usable result. Retry before assessing the estimate."]
    lines += ["", "### What to check next",
        "Confirm the asking price, availability and recorded areas against the source listing, then inspect the sold examples for differences in date, size, title and condition. Building condition, legal constraints and development feasibility are unverified.",
        "", "Only this selected record was checked. Other records may disagree; earlier shortlist requirements have not been independently revalidated by this report. Your conversation remains available for follow-up questions.",
        "", f"[View the checked property record](/property/{property_id})"]
    return Result(text="\n".join(lines), tools_used=tools)
