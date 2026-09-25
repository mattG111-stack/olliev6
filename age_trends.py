"""Descriptive age cohorts, never a causal age adjustment or prediction."""
import math
from statistics import median

from periods import future_sale, sold_year_month


def age_selling_times(rows):
    groups = {"under_3": [], "3_plus": []}
    excluded = 0
    seen = set()
    for row in rows:
        key = (getattr(row, "address", None), row.sold_date, row.sale_price)
        if key[0] and key in seen:
            continue
        ym = sold_year_month(row.sold_date)
        try:
            built = float(row.building_age)
            days = float(row.days_on_market)
        except (ValueError, TypeError):
            excluded += 1
            continue
        # Require a build YEAR, not a present-day age attached to an old sale.
        if (not ym or future_sale(row.sold_date) or not math.isfinite(built)
                or not built.is_integer() or not 1800 <= built <= ym[0]
                or not math.isfinite(days) or not 0 < days <= 730
                or not row.sale_price or row.sale_price <= 0):
            excluded += 1
            continue
        seen.add(key)
        groups["under_3" if ym[0] - built < 3 else "3_plus"].append(days)
    return {
        "groups": [dict(key=k, sales=len(v), median_days=median(v) if v else None)
                   for k, v in groups.items()],
        "excluded_sales": excluded,
        "basis": "recorded_days_on_market",
    }
