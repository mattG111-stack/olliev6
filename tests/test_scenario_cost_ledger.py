"""Read-only synthetic diagnostic: displayed costs must match engine profit.

No database session, source request or production credentials are used.
"""
import os
from pathlib import Path
import sys
from types import SimpleNamespace



import pytest
from routers.properties import ScenarioIn, subdivision_scenario


@pytest.mark.parametrize("zone", [
    "Residential - Mixed Housing Suburban Zone",
    "Residential - Terrace Housing and Apartment Building Zone",
])
@pytest.mark.parametrize("house", [False, True])
@pytest.mark.parametrize("full", [False, True])
def test_displayed_costs_reconcile(zone, house, full):
    listing = SimpleNamespace(
        zoning=zone, land_area_m2=900, buy_price=1_000_000, section_rate=1_500,
        property_type="House" if house else "Section", type_of_title="Freehold",
        address="Synthetic test only", improvement_value_numeric=None,
        land_value_numeric=600_000, cv_numeric=1_000_000,
        beds=3 if house else 0, baths=2 if house else 0,
        floor_area_m2=120 if house else None,
    )
    result = subdivision_scenario(1, ScenarioIn(full_subdivision=full),
                                  SimpleNamespace(get=lambda *args: listing))
    costs = ["services_cost", "selling_cost", "acquisition_cost", "incidentals_cost",
             "demolition_cost", "holding_cost", "contingency_cost", "gst_cost"]
    displayed = result.gross_sales - result.buy_price - sum(getattr(result, k) or 0 for k in costs)
    assert displayed == pytest.approx(result.subdivision_profit, abs=1), result.best_strategy


def test_terrace_services_and_incidentals_change_profit():
    listing = SimpleNamespace(zoning="Residential - Terrace Housing and Apartment Building Zone",
        land_area_m2=900, buy_price=1_000_000, section_rate=1_500, property_type="House",
        type_of_title="Freehold", address="Synthetic only", improvement_value_numeric=None,
        land_value_numeric=600_000, cv_numeric=1_000_000, beds=3, baths=2, floor_area_m2=120)
    db=SimpleNamespace(get=lambda *args: listing)
    base=subdivision_scenario(1, ScenarioIn(), db)
    services=subdivision_scenario(1, ScenarioIn(services_per_section=200_000), db)
    extras=subdivision_scenario(1, ScenarioIn(incidentals_per_section=50_000), db)
    assert services.subdivision_profit < base.subdivision_profit
    assert extras.subdivision_profit < base.subdivision_profit
    assert extras.incidentals_cost == extras.sections * 50_000
