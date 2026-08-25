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


# --- the window must not follow bad data into the future ---------------------

def test_the_month_window_never_runs_past_this_month(db_session):
    """A sold file carrying a future date stretched the chart into months that
    have not happened.

    Reported as a suburb chart running to "Dec 26 · part month" in August. The
    window ended at the LATER of now and the newest month in the data, so one
    stray row four months ahead moved the whole window with it — and the three
    "most recent completed months" behind the improving/softening verdict were
    months nobody had lived through yet.
    """
    from datetime import datetime, timezone
    from app.models import BatchType, ImportBatch, PropertySold
    from app.routers.properties import suburb_stats

    db = db_session
    now = datetime.now(timezone.utc)
    this_month = f"{now.year:04d}-{now.month:02d}"
    future_year = now.year + 1

    batch = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                        filename="future.csv", rows_total=0, is_active=True,
                        status="published")
    db.add(batch); db.flush()
    rows = 0
    # A normal run of sales this month...
    for i in range(8):
        rows += 1
        db.add(PropertySold(slug_id=f"n{i}", address=f"{i} Now St", suburb="Massey",
                            region="Auckland", sale_price=900_000 + i * 1_000,
                            sold_date=f"{this_month}-10", beds=3, baths=1,
                            floor_area_m2=120, land_area_m2=400,
                            import_batch_id=batch.id))
    # ...and one row dated well into the future.
    rows += 1
    db.add(PropertySold(slug_id="future", address="1 Future Rd", suburb="Massey",
                        region="Auckland", sale_price=2_500_000,
                        sold_date=f"{future_year}-12-01", beds=3, baths=1,
                        floor_area_m2=120, land_area_m2=400,
                        import_batch_id=batch.id))
    batch.rows_total = rows
    db.commit()

    stats = suburb_stats(suburb="Massey", region="Auckland", from_year=None,
                         to_year=None, ptype=None, db=db)
    months = [p.month for p in stats.monthly]
    assert months, "no months at all"
    assert max(months) == this_month, (
        f"the chart runs to {max(months)}, which is in the future"
    )
    assert stats.current_month == this_month
