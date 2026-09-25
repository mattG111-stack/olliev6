from types import SimpleNamespace
from age_trends import age_selling_times, age_group
import pytest


def row(year, days, address="a", sale="2025-06-01"):
    return SimpleNamespace(address=address, sold_date=sale, sale_price=800000,
                           building_age=year, days_on_market=days)


def test_boundary_uses_sale_year_and_medians():
    result = age_selling_times([row(2023, 20), row(2024, 40, "b"), row(2022, 60, "c")])
    assert result["groups"] == [dict(key="under_3", sales=2, median_days=30),
                                 dict(key="3_to_8", sales=1, median_days=60),
                                 dict(key="8_plus", sales=0, median_days=None),
                                 dict(key="uncertain", sales=0, median_days=None)]


def test_missing_invalid_future_and_current_age_excluded():
    rows = [row(None, 20), row(5, 20), row(2030, 20), row(2024, None),
            row(2024, float("nan")), row(2024, 0), row(2024, 731),
            row(2024, 20, sale="2099-01-01"), row(2024, 20, sale="unknown")]
    for index, record in enumerate(rows):
        record.address = f"Synthetic {index}"
    result = age_selling_times(rows)
    assert result["excluded_sales"] == 6
    assert result["groups"][-1] == dict(key="uncertain", sales=3, median_days=20)
    assert all(g["sales"] == 0 for g in result["groups"][:-1])


def test_same_sale_deduplicated():
    result = age_selling_times([row(2024, 10), row(2024, 10), row(2024, 50, "b")])
    assert result["groups"][0] == dict(key="under_3", sales=2, median_days=30)


@pytest.mark.parametrize("value,sale,expected", [
    (2024, 2026, "under_3"), (2023, 2026, "3_to_8"),
    (2019, 2026, "3_to_8"), (2018, 2026, "8_plus"),
    ("2020s", 2026, "uncertain"), ("2020", 2026, "uncertain"),
    (2020.0, 2026, "uncertain"), ("2010s", 2026, "uncertain"),
    ("1990s", 2026, "8_plus"), (1990, 2026, "8_plus"),
    (None, 2026, "uncertain"), ("unknown", 2026, "uncertain"),
    ("2024s", 2026, "uncertain"), (float("nan"), 2026, "uncertain"),
    (2027, 2026, "uncertain"), (3, 2026, "uncertain"),
])
def test_age_precision_and_boundaries(value, sale, expected):
    assert age_group(value, sale) == expected


def test_decade_records_do_not_inflate_older_group():
    result = age_selling_times([row("2020", 30), row("2020s", 40, "b"),
                               row("1990s", 50, "c")])
    assert result["groups"][0]["sales"] == 0
    assert result["groups"][1]["sales"] == 0
    assert result["groups"][2] == dict(key="8_plus", sales=1, median_days=50)
    assert result["groups"][3] == dict(key="uncertain", sales=2, median_days=35)
