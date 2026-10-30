"""New construction and retained building value are independent assumptions."""
from dataclasses import replace

import pytest

from pricing.subdivision import (
    SubdivisionAssumptions, THAB_TERRACE_FLOOR, compute,
)


def terrace(assumptions=None):
    return compute(
        zone="Residential - Terrace Housing and Apartment Building Zone",
        land_area=900, buy_price=1_000_000, section_rate=1_500,
        title_type="Freehold", beds=3, floor_area=120,
        assumptions=assumptions,
    )


def test_default_terrace_cost_preserves_existing_2800_rate():
    result = terrace()
    assert result.section_value_method == "thab_terraces"
    assert result.dwellings > 0
    assert result.services_cost == result.dwellings * (105 * 2_800 + 50_000)


def test_retained_replacement_override_does_not_change_new_build_cost_or_profit():
    defaults = SubdivisionAssumptions()
    assert terrace(defaults) == terrace(replace(defaults, build_rate=5_000))


def test_higher_construction_rate_reduces_profit_by_actual_build_cost():
    assumptions = SubdivisionAssumptions(holding_rate=0, contingency_rate=0, gst_rate=0)
    base = terrace(assumptions)
    higher = terrace(replace(assumptions, townhouse_build_rate=3_200))
    extra = base.dwellings * THAB_TERRACE_FLOOR * 400
    assert higher.services_cost - base.services_cost == extra
    assert base.subdivision_profit - higher.subdivision_profit == extra
    assert base.gross_sales == higher.gross_sales


def test_new_build_override_does_not_change_retained_house_scenario():
    kwargs = dict(
        zone="Residential - Mixed Housing Suburban Zone", land_area=900,
        buy_price=1_000_000, section_rate=1_500, title_type="Freehold",
        beds=3, floor_area=120,
    )
    base = compute(**kwargs)
    assert base.can_subdivide
    assert base == compute(**kwargs, assumptions=SubdivisionAssumptions(townhouse_build_rate=5_000))


@pytest.mark.parametrize("rate", [0, -1, float("inf"), float("nan")])
def test_construction_rate_rejects_invalid_values(rate):
    with pytest.raises(ValueError):
        SubdivisionAssumptions(townhouse_build_rate=rate)


def test_scenario_request_has_independent_validated_rate():
    from pydantic import ValidationError
    from routers.properties import ScenarioIn

    request = ScenarioIn(build_rate=4_000, townhouse_build_rate=2_950)
    assert request.build_rate == 4_000
    assert request.townhouse_build_rate == 2_950
    assert ScenarioIn().townhouse_build_rate is None
    for rate in [0, -1, float("inf"), float("nan")]:
        with pytest.raises(ValidationError):
            ScenarioIn(townhouse_build_rate=rate)
