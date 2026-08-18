"""A suburb's monthly line must show the market, not which homes happened to sell.

"Where the market is heading" drew a 30% sawtooth month to month. A market does
not do that. Two faults were behind it.

The headline series defaulted to min_sales=1, so a month with a single sale
plotted that one house's price as the suburb's median.

And the metric itself cannot do the job. Measured on generated sales for a
mixed-stock suburb, three-month smoothed: a median PRICE line swung 74% in a
market held deliberately flat and 89% in one rising 19% a year — the two are
indistinguishable, because what moves a suburb median is the mix of what sold.
The same suburb measured against CV moved ~9pp flat and ~15pp rising. Every sale
is compared to its own valuation, so the mix cancels.

These tests hold both: the noise floor stays low, and a real move still clears it.
"""
from __future__ import annotations

import random
import statistics

import pytest

from app.routers.properties import MIN_MONTH_SALES, _month_series

MONTHS = [f"2026-{m:02d}" for m in range(1, 13)]


class _Sale:
    def __init__(self, price, cv):
        self.sale_price = price
        self.cv_numeric = cv
        self.days_on_market = 30


def _suburb(sales_per_month: int, drift_pct_per_month: float, seed: int = 7):
    """Mixed stock — units through to large houses — so the mix can bite."""
    rnd = random.Random(seed)
    by_month: dict[str, list] = {}
    for i, month in enumerate(MONTHS):
        n = max(1, int(rnd.gauss(sales_per_month, 1)))
        rows = []
        for _ in range(n):
            cv = 1_400_000 * rnd.choice([0.5, 0.55, 1.0, 1.5])
            level = cv * ((1 + drift_pct_per_month / 100) ** i)
            rows.append(_Sale(max(200_000, rnd.gauss(level, cv * 0.08)), cv))
        by_month[month] = rows
    return by_month


def _vs_cv(points):
    return [p.sale_vs_cv for p in points if p.sale_vs_cv is not None]


@pytest.mark.parametrize("sales_per_month", [3, 6, 12])
def test_a_flat_market_reads_as_flat(sales_per_month):
    """Any movement here is sampling noise: the underlying level never changes."""
    pts = _month_series(_suburb(sales_per_month, 0), MONTHS, min_sales=MIN_MONTH_SALES)
    v = _vs_cv(pts)
    assert len(v) > 1
    noise_pp = (max(v) - min(v)) * 100
    assert noise_pp < 12, f"a flat market swung {noise_pp:.1f}pp"


@pytest.mark.parametrize("sales_per_month", [3, 6, 12])
def test_a_real_rise_still_shows(sales_per_month):
    """Smoothing must not buy its quiet line by flattening the signal too.

    An earlier attempt widened the window until it held 25 sales. It cut flat-
    market noise from 33% to 17% and turned this +19% into MINUS 7%, because the
    first and last windows ended up pooling almost the same sales.
    """
    flat = _vs_cv(_month_series(_suburb(sales_per_month, 0), MONTHS,
                                min_sales=MIN_MONTH_SALES))
    rising = _vs_cv(_month_series(_suburb(sales_per_month, 1.5), MONTHS,
                                  min_sales=MIN_MONTH_SALES))
    noise_pp = (max(flat) - min(flat)) * 100
    move_pp = (rising[-1] - rising[0]) * 100
    assert move_pp > 0, f"a rising market read as {move_pp:.1f}pp"
    assert move_pp > noise_pp, (
        f"a real +19% year moved {move_pp:.1f}pp, inside the {noise_pp:.1f}pp "
        f"noise floor — the trend is not visible above the sampling"
    )


def test_one_sale_cannot_be_a_suburbs_median():
    """The default that caused it: min_sales=1."""
    by_month = {m: [] for m in MONTHS}
    by_month["2026-06"] = [_Sale(4_000_000, 4_000_000)]     # one trophy sale
    pts = _month_series(by_month, MONTHS, min_sales=MIN_MONTH_SALES)
    june = next(p for p in pts if p.month == "2026-06")
    assert june.median_price is None, "a single sale was plotted as the median"
    assert june.sales == 1, "the sale should still be counted as activity"


def test_sales_counts_are_not_smoothed():
    """Price pools three months; 'how busy was this month' must not."""
    by_month = {m: [] for m in MONTHS}
    for m in ("2026-04", "2026-05", "2026-06"):
        by_month[m] = [_Sale(1_400_000, 1_400_000) for _ in range(4)]
    pts = {p.month: p for p in _month_series(by_month, MONTHS, min_sales=MIN_MONTH_SALES)}
    assert pts["2026-06"].sales == 4, "activity was blended across months"
    assert pts["2026-07"].sales == 0
    # ...while the price still carries across the window.
    assert pts["2026-06"].median_price == 1_400_000
