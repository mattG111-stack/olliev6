"""Let the operator say how many lots the site really takes.

    "i need to be able to edit how many it can be subdivided up into as they
     have more profit than we are giving them"

Every other figure in the subdivision calculator is an assumption about COST —
build rate, selling percentage, holding, GST. This one is about the SITE, and it
is the one the model is least able to see.

The count is land area divided by the zone's minimum lot, less an allowance for
roading. That is a floor, not a ceiling. It cannot know about a corner section
with two frontages, an existing right of way, a boundary that already suits
three, or a resource consent somebody has already been granted. A developer
looking at the title knows things this arithmetic does not.

And profit is close to linear in the count, so a model that is one lot low
understates the deal by about a third — and it is low far more often than high,
because every unknown it cannot see adds lots rather than removing them.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")

import pytest  # noqa: E402

from app.pricing import assumptions as A  # noqa: E402
from app.pricing import subdivision as SD  # noqa: E402

SITE = dict(zone="Residential - Mixed Housing Suburban Zone", land_area=1400.0,
            buy_price=1_200_000.0, section_rate=1200.0, property_type="House",
            title_type="Freehold", beds=3, baths=1, floor_area=140.0,
            cv=1_300_000.0)


def test_without_an_override_nothing_changes():
    """The guard on everything below: the default has to stay the default."""
    assert SD.compute(**SITE).sections == SD.compute(**SITE, lots_override=None).sections


def test_the_operator_count_is_used():
    assert SD.compute(**SITE, lots_override=5).sections == 5
    assert SD.compute(**SITE, lots_override=4).sections == 4


def test_the_extra_lots_reach_the_profit():
    """Setting the number and having it change nothing would be worse than not
    offering it — the whole complaint is that the profit is understated."""
    base = SD.compute(**SITE)
    more = SD.compute(**SITE, lots_override=(base.sections or 0) + 2)

    assert more.sections == (base.sections or 0) + 2
    assert more.max_addl_lots == more.sections - 1
    assert more.gross_sales > base.gross_sales
    assert more.subdivision_profit > base.subdivision_profit


def test_the_additional_count_stays_one_less_than_the_total():
    """max_addl_lots is "sections - 1" everywhere. A terrace path once returned
    the full count there and reported one extra title as two."""
    for n in (2, 3, 5, 8):
        assert SD.compute(**SITE, lots_override=n).max_addl_lots == n - 1


# ---- what an override is not ------------------------------------------------
def test_it_is_still_capped_at_what_is_practical():
    """Local knowledge, not a licence to put forty houses on a quarter acre."""
    out = SD.compute(**SITE, lots_override=999)
    assert out.sections <= A.MAX_PRACTICAL_LOTS_TOTAL


@pytest.mark.parametrize("silly", [0, -3, 0.4])
def test_a_nonsense_count_does_not_produce_a_nonsense_site(silly):
    """Zero or negative lots is not a subdivision. It must not come back as one,
    and it must not raise either — this runs behind a text box."""
    out = SD.compute(**SITE, lots_override=silly)
    assert out.sections is None or out.sections >= 2


def test_a_site_that_cannot_be_subdivided_at_all_stays_that_way():
    """An override says how many lots fit, not whether the zone allows any. A
    text box must not turn a rural block into a subdivision."""
    rural = dict(SITE, zone="Rural - Rural Production Zone", land_area=900.0)
    assert SD.compute(**rural, lots_override=6).is_subdividable is False


# ---- and it has to reach the endpoint the page calls ------------------------
def test_the_scenario_endpoint_accepts_it():
    from app.routers.properties import ScenarioIn

    assert "lots" in ScenarioIn.model_fields
    assert ScenarioIn(lots=5).lots == 5
    assert ScenarioIn().lots is None


def test_the_endpoint_passes_it_to_the_calculation():
    """Accepting a field and ignoring it is the failure this whole file exists
    to prevent — the number changes on screen and the profit does not."""
    import inspect

    from app.routers import properties

    src = inspect.getsource(properties.subdivision_scenario)
    assert "lots_override=body.lots" in src
