"""22 Weybridge Crescent: 400 m², already subdivided, offered as a subdivision.

The THAB path does not use the zone's minimum lot. It asks how many terraces
fit — FLOOR(land × 0.85 ÷ 120 m²) — and calls the site subdividable at two. That
is 283 m², smaller than a single already-subdivided lot, so a 400 m² section
carrying the house the last subdivision built came back as "+1 lot, build 2
terraces": knock down what was just finished and start again.

The 120 m² figure is not wrong. It is the fee-simple land a terrace sits on, and
it exists because real terrace sites were being missed — 42 Cape Road, Mangere,
769 m² consented for FIVE terraces, which the zone's nominal 1,200 m² minimum
returned as "not subdividable". What went wrong is that one number was answering
two different questions: what fits on a site, and which sites are worth
developing. Those need separate answers.

The answer is one floor over every zone: under 600 m², a section is a section.
Measured, that changes THAB and nothing else — Single House, Large Lot and Rural
are never subdividable at any size, and Mixed Housing already needed 667 m². But
Mixed Housing's 667 is not a rule anyone wrote down; it falls out of a 300 m²
minimum lot and a 10% road allowance, and it moves if either is ever tuned. A
stated floor cannot drift.

Across 23 staged exports, 1,246 flagged rows sat at 600 m² or less — 4.2% of
every subdividable flag, and every one of them THAB.
"""
from __future__ import annotations

import pytest

from app.pricing import subdivision as SD
from app.pricing.subdivision import THAB_MIN_SITE_M2, compute

THAB = "Residential - Terrace Housing and Apartment Building Zone"


def site(**kw):
    """A freehold THAB section with a house on it. Land area is the variable."""
    d = dict(zone=THAB, land_area=769, buy_price=550_000, section_rate=1500,
             title_type="Freehold", property_type="House", cv=900_000,
             beds=3, baths=1, floor_area=120, improvement_value=300_000,
             land_value=600_000)
    d.update(kw)
    return compute(**d)


# ---- the site floor ---------------------------------------------------------
def test_the_already_subdivided_lot_is_not_a_subdivision():
    """22 Weybridge Crescent itself."""
    r = site(land_area=400)
    assert r.is_subdividable is False
    assert r.section_value_method in ("thab_site_too_small", "site_too_small")
    assert r.max_addl_lots is None
    assert r.subdivision_profit is None


@pytest.mark.parametrize("land", [283, 300, 400, 450, 500, 550, 599])
def test_nothing_under_the_floor_is_flagged(land):
    """Under 600 m², a section is a section — in every zone, not just this one."""
    assert site(land_area=land).is_subdividable is False


@pytest.mark.parametrize("land", [600, 700, 769, 1200])
def test_a_real_site_above_the_floor_still_works(land):
    r = site(land_area=land)
    assert r.is_subdividable is True
    assert r.section_value_method == "thab_terraces"


def test_the_site_that_this_model_exists_for_still_qualifies():
    """42 Cape Road, Mangere: 769 m², consented (LUC60402550 / SUB60402551) to
    five terraces. Raising the floor must not undo the fix that put it back."""
    r = site(land_area=769)
    assert r.is_subdividable is True
    assert r.sections == 5, f"consented for five, modelled {r.sections}"
    assert r.max_addl_lots == 4.0          # ADDITIONAL lots, not the total


def test_over_the_floor_means_room_for_a_real_development():
    """What the floor is really buying: room for a development, not a squeeze.
    The smallest qualifying site fits four terraces, which is what makes
    knocking a house down worth doing."""
    assert site(land_area=THAB_MIN_SITE_M2).sections >= 4


def test_the_floor_is_checked_before_the_terrace_count():
    """"It fits two terraces" is true of a 283 m² lot and answers the wrong
    question, so the site size has to be asked first."""
    assert site(land_area=283).section_value_method in (
        "thab_site_too_small", "site_too_small")


# ---- a house is a house, however the record spells it -----------------------
def test_a_house_is_seen_from_its_floor_area_alone():
    """has_dwelling read beds-or-baths only. A record missing both — common in
    the weekly feed — was treated as bare land, so the demolition never entered
    the profit and the strategy said "Build" on an occupied section."""
    r = site(beds=None, baths=None, floor_area=120, improvement_value=None)
    assert "Demolish" in r.best_strategy


def test_a_house_is_seen_from_its_improvement_value_alone():
    r = site(beds=None, baths=None, floor_area=None, improvement_value=300_000)
    assert "Demolish" in r.best_strategy


def test_missing_the_demolition_overstated_the_profit():
    """The cost of the mistake, in dollars: the same site priced with and
    without the house the record failed to mention."""
    with_house = site(beds=None, baths=None, floor_area=120,
                      improvement_value=300_000)
    bare = site(beds=None, baths=None, floor_area=None, improvement_value=None)
    assert bare.subdivision_profit > with_house.subdivision_profit
    assert "Demolish" not in bare.best_strategy


def test_genuinely_bare_land_is_still_bare():
    """The fix must not invent a house on an empty section."""
    r = site(beds=None, baths=None, floor_area=None, improvement_value=None)
    assert "Demolish" not in r.best_strategy
    assert r.is_subdividable is True


def test_zero_is_not_a_building():
    """A recorded 0 m² floor and a $0 improvement value both mean no building,
    not a building of no size."""
    assert SD._has_dwelling(0, 0, 0, 0) is False
    assert SD._has_dwelling(None, None, None, None) is False
    assert SD._has_dwelling(None, None, 120, None) is True


def test_a_bad_number_does_not_conjure_a_dwelling():
    assert SD._has_dwelling("", "n/a", None, None) is False
    assert SD._has_dwelling(float("nan"), None, None, None) is False


# ---- the other zones are untouched ------------------------------------------
def test_a_mixed_housing_site_over_the_floor_still_splits():
    """The floor must not cost the zone that was always working. Mixed Housing
    needs 667 m² on its own arithmetic, so the floor is never the binding
    constraint there — but it must not break it either."""
    mhs = "Residential - Mixed Housing Suburban Zone"
    r = compute(zone=mhs, land_area=700, buy_price=550_000, section_rate=1500,
                title_type="Freehold", property_type="House", cv=900_000,
                beds=3, baths=1, floor_area=120, improvement_value=300_000)
    assert r.section_value_method != "thab_site_too_small"
    assert r.is_subdividable is True, "a 700 m² Mixed Housing site still splits"


def test_a_single_house_zone_is_still_never_subdividable():
    r = compute(zone="Residential - Single House Zone", land_area=1200,
                buy_price=550_000, section_rate=1500, title_type="Freehold",
                property_type="House", cv=900_000, beds=3, baths=1)
    assert r.is_subdividable is False


@pytest.mark.parametrize("zone", [
    "Residential - Mixed Housing Suburban Zone",
    "Residential - Mixed Housing Urban Zone",
    "Residential - Terrace Housing and Apartment Building Zone",
])
def test_the_floor_applies_to_every_zone_not_just_thab(zone):
    """Written as a rule over all of them on purpose. Mixed Housing does not
    reach 600 m² today, but that is an accident of its road allowance rather
    than a decision, and an accident can be tuned away without anyone noticing."""
    r = compute(zone=zone, land_area=599, buy_price=550_000, section_rate=1500,
                title_type="Freehold", property_type="House", cv=900_000,
                beds=3, baths=1, floor_area=120, improvement_value=300_000)
    assert r.is_subdividable is False


def test_the_two_floors_are_one_number():
    """THAB checks its own, so they have to be the same number or the rule has
    two answers depending on which path a property takes."""
    from app.pricing.subdivision import MIN_SUBDIVIDABLE_SITE_M2

    assert THAB_MIN_SITE_M2 == MIN_SUBDIVIDABLE_SITE_M2 == 600.0
