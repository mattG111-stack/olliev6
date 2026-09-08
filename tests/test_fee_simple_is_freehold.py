"""One estate, three spellings, and the app disagreed with itself about it.

    "i cant edit how many sections you are going to get out of a subdivision"

The lot editor was there. On a great many properties the CALCULATOR it lives in
was not, and this is why.

"Fee simple" is freehold — the same estate, the older legal name, and what a
fair amount of real listing data says. Three separate rules read the title:

    pricing/glm.title_code        "freehold" / "1"                 -> 1
    pricing/buyprice._title_bucket  numeric, else via title_code
    ml/features._title_bucket     "freehold" / "fee simple" ...    -> FH

Only the last one knew the synonym. So a listing titled "Fee Simple" was
freehold to the trained valuation and "Other" to everything else — including
the subdivision model, which requires a positively-known freehold. Not
freehold means not subdividable; not subdividable means no subdivision figures
on the row; and the property page only shows the calculator when the stored
section count is 2 or more. So the page had nothing to edit, and the reason was
a missing word in a lookup table three steps away.

The synonym now lives in the mapping every one of those rules goes through.
"""
from __future__ import annotations

import pytest

from app.pricing.buyprice import _title_bucket
from app.pricing.glm import title_code
from app.pricing.subdivision import compute

FREEHOLD_SPELLINGS = ["Freehold", "freehold", "FREEHOLD",
                      "Fee Simple", "fee simple", "fee-simple", "1"]
NOT_FREEHOLD = ["Cross-Lease", "Cross Lease", "Leasehold", "Unit Title", "Stratum"]


def _sections(title):
    return compute(
        zone="Residential - Mixed Housing Suburban Zone", land_area=1400.0,
        buy_price=1_100_000.0, section_rate=1800.0, property_type="House",
        title_type=title, land_value=760_000.0, cv=1_150_000.0,
    ).sections


@pytest.mark.parametrize("title", FREEHOLD_SPELLINGS)
def test_every_way_of_writing_freehold_can_subdivide(title):
    """THE BUG. "Fee Simple" produced no sections, so no calculator, so nothing
    to edit — on a site that is plainly subdividable."""
    assert _sections(title) == 3, f"{title!r} was not read as freehold"


@pytest.mark.parametrize("title", FREEHOLD_SPELLINGS)
def test_the_three_title_rules_agree(title):
    """The fault was not the missing word so much as three rules for one idea.
    A synonym added to one of them would put this straight back."""
    assert title_code(title) == 1, f"title_code does not know {title!r}"
    assert _title_bucket(title) == "FH", f"_title_bucket does not know {title!r}"


def test_the_valuation_and_the_subdivision_model_read_it_the_same_way():
    """These two disagreeing about the same house is what made it invisible:
    freehold enough to be priced, not freehold enough to be developed."""
    from app.ml.features import title_bucket as ml_bucket

    for title in FREEHOLD_SPELLINGS:
        assert ml_bucket(title) == _title_bucket(title) == "FH", title


@pytest.mark.parametrize("title", NOT_FREEHOLD)
def test_a_title_that_cannot_be_divided_still_cannot(title):
    """The counterweight, and it matters more than the fix: a cross-lease owner
    cannot divide the land, and inventing a subdivision on one would be a
    number we could not defend."""
    assert _sections(title) is None, f"{title!r} was allowed to subdivide"


@pytest.mark.parametrize("title", [None, "", "   "])
def test_a_missing_title_is_still_not_an_answer(title):
    """Absent is not freehold. Guessing the permissive way on the one attribute
    that decides eligibility outright is how a cross-lease becomes a
    development opportunity."""
    assert _sections(title) is None
