"""Sale methods from the advertised price line, never from marketing prose."""
import re
import math
from portals.page_data import number


def apply_sale_method(record):
    # A historic sold price does not establish how the home was marketed.
    if record.get("listing_kind") != "for_sale":
        return
    display = str(record.get("price_display") or "").strip()
    fixed = re.fullmatch(r"(?:asking(?: price)?\s*)?(\$[\d,.]+[km]?)(?:\s+negotiable)?", display, re.I)
    rules = (
        (r"\bdutch auction\b", "dutch auction"),
        (r"\bauction\b", "auction"),
        (r"\bdeadline(?: sale| treaty| private treaty)?\b", "deadline sale"),
        (r"\btender\b", "tender"),
        (r"\b(?:by negotiation|negotiation|negotiable|price on application|poa|contact agent|expressions of interest|eoi)\b", "negotiation"),
        (r"\b(?:enquiries over|offers over|offers above|buyer enquiry over|buyer budget over)\b", "offers over"),
    )
    method = "fixed" if fixed else next((value for pattern, value in rules if re.search(pattern, display, re.I)), None)
    if method:
        record["sale_method"] = method
        advertised = fixed[1] if fixed else None
        if method == "offers over":
            guide = re.fullmatch(r"(?:enquiries over|offers over|offers above|buyer enquiry over|buyer budget over)\s*(\$[\d,.]+[km]?)", display, re.I)
            advertised = guide[1] if guide else None
        amount = number(advertised) if advertised else None
        if amount is not None and math.isfinite(amount) and amount > 0:
            record["price_numeric"] = amount
        else:
            # Keep the displayed guide as evidence, not a firm asking price.
            record.pop("price_numeric", None)
