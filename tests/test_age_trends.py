from types import SimpleNamespace
from age_trends import age_selling_times


def row(year, days, address="a", sale="2025-06-01"):
    return SimpleNamespace(address=address, sold_date=sale, sale_price=800000,
                           building_age=year, days_on_market=days)


def test_boundary_uses_sale_year_and_medians():
    result = age_selling_times([row(2023, 20), row(2024, 40, "b"), row(2022, 60, "c")])
    assert result["groups"] == [dict(key="under_3", sales=2, median_days=30),
                                 dict(key="3_plus", sales=1, median_days=60)]


def test_missing_invalid_future_and_current_age_excluded():
    rows = [row(None, 20), row(5, 20), row(2030, 20), row(2024, None),
            row(2024, float("nan")), row(2024, 0), row(2024, 731),
            row(2024, 20, sale="2099-01-01"), row(2024, 20, sale="unknown")]
    result = age_selling_times(rows)
    assert result["excluded_sales"] == len(rows)
    assert all(g["sales"] == 0 and g["median_days"] is None for g in result["groups"])


def test_same_sale_deduplicated():
    result = age_selling_times([row(2024, 10), row(2024, 10), row(2024, 50, "b")])
    assert result["groups"][0] == dict(key="under_3", sales=2, median_days=30)
