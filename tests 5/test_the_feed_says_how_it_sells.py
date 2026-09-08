"""The export says how the house is being sold. Read it.

    "718 Remuera Road, Remuera, Auckland City is an auction ?"

It is. The feed says so outright — `sale_method` is `auction`, the field the
agent filled in. But the classifier never looked at that column. It read the
price text, saw `$4.25M`, and returned `fixed`: a house going to auction,
published as though a vendor had named an asking price.

And the price it "asked" was $4,250,000 — its council valuation, to the dollar.
No vendor named that. It is the CV, carried into the price field because an
auction listing has no price to put there.

Across one daily export that was **1,128 listings out of 1,586**. Every one a
house with no asking price, priced as though it had one, and 277 of them
"asking" exactly their CV.

The classifier already knew this was the danger — its own docstring cites 48A
Garnet Road, advertised by negotiation everywhere, arriving as "$2,350,000"
against a $3.5M CV and being published as a 49% margin. It defended against it
by reading the price TEXT for phrases like "auction" and "by negotiation". That
works only when the words happen to appear in the price line. Here they appear
in a column of their own, and inferring from the shadow while ignoring the
writing on the wall let the whole thing through.

So sale_method is read first, and when it names a no-price method that is final.
When it claims a fixed price it still has to prove one, because a fixed-price
listing with no number is not a price either.
"""
from __future__ import annotations

import pytest

from app.ingest import _detect_listing_type as detect


# ---- the property that started it -------------------------------------------
def test_718_remuera_road_is_an_auction():
    """Exactly as it arrives in the file: sale_method says auction, price_display
    shows the CV as though it were an asking price."""
    assert detect("$4.25M", "4250000", "auction") == "auction"


def test_it_was_previously_read_as_a_fixed_asking_price():
    """The bug, pinned. Without the feed's own column this is 'fixed' — and a
    'fixed' listing is the only kind allowed to drive the asking x 0.95 price
    path, so the CV became the anchor for the valuation."""
    assert detect("$4.25M", "4250000") == "fixed"


# ---- what the feed says wins ------------------------------------------------
@pytest.mark.parametrize("method,expected", [
    ("auction", "auction"),
    ("Auction", "auction"),
    ("  AUCTION  ", "auction"),
    ("tender", "tender"),
    ("deadline treaty", "tender"),
    ("deadline sale", "tender"),
    ("by negotiation", "negotiation"),
])
def test_a_stated_no_price_method_is_final(method, expected):
    """Whatever the price line shows — and these all show something, that is the
    whole problem — the method the agent recorded decides."""
    assert detect("$2,350,000", "2350000", method) == expected


# A method that publishes a FLOOR is not a no-price method, and filing it as one
# threw away a figure the vendor did name.
#
#     "And where it gives a price like this we can use it as a asking price"
#     "we can use that as the asking price for those listing but mark that it
#      is from that price"
#
# 10 Fernbird Place advertises "Enquiries over $699,000" and we showed nothing.
# The number is published now, marked as the floor it is — see
# test_a_guide_price_is_a_real_number.py for what that changes and, more
# importantly, for the three things it deliberately does not.
@pytest.mark.parametrize("method", [
    "enquiries over", "offers over", "buyer enquiry over",
    "buyer budget over", "negotiable from",
])
def test_a_stated_floor_keeps_its_number(method):
    assert detect("$2,350,000", "2350000", method) == "guide"


@pytest.mark.parametrize("method", [
    "enquiries over", "offers over", "buyer enquiry over",
    "buyer budget over", "negotiable from",
])
def test_a_stated_floor_with_no_number_names_nothing(method):
    """"Offers over" and nothing after it is a by-negotiation listing."""
    assert detect("Contact agent", None, method) == "negotiation"


def test_every_sale_method_in_the_real_file_is_understood():
    """The values seen in one day's export. A method we do not recognise falls
    back to reading the price text, which is how this bug happened — so an
    unrecognised value is worth failing on rather than silently tolerating."""
    seen = ["by negotiation", "auction", "fixed price", "deadline treaty",
            "tender", "enquiries over", "offers over", "asking price",
            "buyer enquiry over", "negotiable from", "buyer budget over"]
    unhandled = [m for m in seen
                 if m not in ("fixed price", "asking price")
                 and detect("$1,000,000", "1000000", m) == "fixed"]
    assert unhandled == [], f"read as a fixed asking price: {unhandled}"


# ---- a claimed fixed price still has to prove itself ------------------------
def test_a_fixed_price_with_a_real_number_is_a_fixed_price():
    assert detect("$1,250,000", "1250000", "fixed price") == "fixed"


def test_a_fixed_price_claim_with_no_number_is_not_a_price():
    """The feed saying "fixed price" does not conjure one. Falls through to the
    text, which says to contact the agent."""
    assert detect("Contact agent", None, "fixed price") == "negotiation"


def test_an_empty_sale_method_falls_back_to_the_old_reading():
    """Older files and other sources carry no such column, and they have to keep
    working exactly as before."""
    assert detect("Auction 12 June", None, None) == "auction"
    assert detect("$829,000", "829000", None) == "fixed"
    assert detect("By negotiation", "2350000", "") == "negotiation"


# ---- why it matters ---------------------------------------------------------
def test_only_a_fixed_listing_may_use_the_asking_price():
    """The reason the classification decides anything at all: LISTING_TYPES_WITH
    _ASKING is {"fixed"}, so everything else is valued by the model rather than
    anchored to a number the vendor never named."""
    from app.pricing.glm import LISTING_TYPES_WITH_ASKING

    assert LISTING_TYPES_WITH_ASKING == frozenset({"fixed"})
    for method in ("auction", "tender", "by negotiation"):
        assert detect("$4.25M", "4250000", method) not in LISTING_TYPES_WITH_ASKING


def test_the_pipeline_passes_the_column_through():
    """The classifier reading sale_method is no use if the caller never hands it
    over — and the pipeline is the only caller that matters."""
    from pathlib import Path

    src = Path("app/pricing/pipeline.py").read_text()
    call = src[src.index("listing_type = _detect_listing_type"):][:220]
    assert "sale_method" in call, "the pipeline still classifies without it"
