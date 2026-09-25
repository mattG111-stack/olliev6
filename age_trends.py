"""Descriptive age cohorts, never a causal age adjustment or prediction."""
import math
import re
from statistics import median

from periods import future_sale, sold_year_month


def age_group(value, sale_year):
    """Never turn an ambiguous decade start into an exact build year.

    Legacy imports lost precision: a bare 2020 can mean 2020s. Treat numeric
    decade starts conservatively as ranges too. Only classify a range when
    every possible build year fits the same cohort. Other numeric years remain
    calendar-year approximations, not exact birthdays.
    """
    raw = str(value).strip().lower()
    decade = re.fullmatch(r"(\d{4})s", raw)
    try:
        year = float(decade[1] if decade else raw)
    except (TypeError, ValueError):
        return "uncertain"
    if not math.isfinite(year) or not year.is_integer() or not 1800 <= year <= sale_year:
        return "uncertain"
    year = int(year)
    if decade and year % 10:
        return "uncertain"
    latest = min(year + 9, sale_year) if decade or year % 10 == 0 else year
    def band(age):
        return "under_3" if age < 3 else "3_to_8" if age < 8 else "8_plus"
    young, old = band(sale_year - latest), band(sale_year - year)
    return young if young == old else "uncertain"


def age_selling_times(rows):
    groups = {"under_3": [], "3_to_8": [], "8_plus": [], "uncertain": []}
    excluded = 0
    seen = set()
    for row in rows:
        key = (getattr(row, "address", None), row.sold_date, row.sale_price)
        if key[0] and key in seen:
            continue
        ym = sold_year_month(row.sold_date)
        try:
            days = float(row.days_on_market)
        except (ValueError, TypeError):
            excluded += 1
            continue
        if (not ym or future_sale(row.sold_date)
                or not math.isfinite(days) or not 0 < days <= 730
                or not row.sale_price or row.sale_price <= 0):
            excluded += 1
            continue
        seen.add(key)
        groups[age_group(row.building_age, ym[0])].append(days)
    return {
        "groups": [dict(key=k, sales=len(v), median_days=median(v) if v else None)
                   for k, v in groups.items()],
        "excluded_sales": excluded,
        "basis": "recorded_days_on_market",
    }
