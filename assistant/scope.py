"""Shared assistant scope responses and request checks."""
import re

RENTAL_REPLY = ("Ollie covers buying, selling, property values and sales analytics. "
                "Rental data is coming soon. For now, ask me about properties for sale, recent sales or market trends.")


def rental_request(question: str) -> bool:
    # Excluding rentals from a sales search is still a supported sales question.
    text = re.sub(r"\b(?:not|no|exclude|excluding)\s+rentals?\b", "", question, flags=re.I)
    return bool(re.search(r"\b(?:rent|rents|rental|rentals|renting|tenant|tenants|tenancy|landlord|landlords)\b", text, re.I))

