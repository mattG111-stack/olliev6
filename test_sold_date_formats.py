"""Both stored spellings of a sale date must work everywhere.

Ingest canonicalises sale dates to ISO so one sale cannot be stored several
times under different spellings. Three places downstream had been written
against the scraper's M/D/YYYY text, and none of them failed loudly when handed
ISO — each just quietly did the wrong thing:

  * the comparables filter matched '%/2024' and returned NO comps at all
  * _period returned None, emptying the month buckets behind the trends
  * the buy-price engine split on "/", got one column, and skipped its recency
    bound entirely — so 1990s sales priced today's houses

Silence is what makes these worth a test. An empty comp set looks like a thin
suburb, and a missing recency bound looks like a working valuation.
"""
from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import or_

from app.models import BatchType, ImportBatch, PropertySold
from app.periods import _period, sold_year_month
from app.pricing import buyprice as B


@pytest.mark.parametrize("raw,expected", [
    ("5/14/2026", (2026, 5)),          # scraper's M/D/YYYY
    ("2026-05-14", (2026, 5)),         # ISO from ingest
    ("2026-05-14 00:00:00", (2026, 5)),  # ISO with a time
    ("2026-05-14T09:30:00", (2026, 5)),  # ISO with a T separator
    ("12/1/2019", (2019, 12)),
    ("2019-12-01", (2019, 12)),
    ("junk", None),
    ("", None),
    (None, None),
    ("13/1/2026", None),               # month 13 is not a date
])
def test_sold_year_month_reads_both_formats(raw, expected):
    assert sold_year_month(raw) == expected


def test_period_agrees_across_formats():
    assert _period("5/14/2026") == _period("2026-05-14") == "2026-05"


def test_comparables_year_filter_matches_both_formats(db_session):
    """The filter that returned zero comps for every listing after ISO landed."""
    db = db_session
    batch = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                        filename="mixed.csv", rows_total=5, is_active=True,
                        status="published")
    db.add(batch); db.flush()
    for slug, date in [("iso24", "2024-03-01"), ("iso26", "2026-07-31"),
                       ("legacy", "6/1/2025"), ("old", "2019-01-01"),
                       ("legacyold", "6/1/2019")]:
        db.add(PropertySold(slug_id=slug, address=f"{slug} St", suburb="Remuera",
                            region="Auckland", sale_price=1_000_000,
                            sold_date=date, beds=3, baths=1,
                            floor_area_m2=120, land_area_m2=500,
                            import_batch_id=batch.id))
    db.commit()

    years = ("2024", "2025", "2026")
    matched = db.query(PropertySold).filter(or_(*(
        [PropertySold.sold_date.like(f"%/{y}") for y in years]
        + [PropertySold.sold_date.like(f"{y}-%") for y in years]
    ))).all()

    assert {m.slug_id for m in matched} == {"iso24", "iso26", "legacy"}, (
        "the recency filter missed one of the two stored date formats"
    )


def test_buyprice_recency_bound_applies_to_iso_dates():
    """Splitting on '/' left ISO rows unfiltered — every sale ever entered."""
    dates = ["1993-11-30", "2019-06-15", "2025-01-10",
             "2025-06-30", "2025-07-01", "2026-07-31"]
    ym = pd.Series(dates).map(sold_year_month)
    yy = pd.to_numeric(ym.map(lambda v: v[0] if v else None), errors="coerce")
    mm = pd.to_numeric(ym.map(lambda v: v[1] if v else None), errors="coerce")
    recent = ((yy > B.SOLD_FROM_YEAR)
              | ((yy == B.SOLD_FROM_YEAR) & (mm >= B.SOLD_FROM_MONTH)))

    kept = [d for d, k in zip(dates, recent) if k]
    assert kept == ["2025-07-01", "2026-07-31"], (
        f"recency bound (on/after {B.SOLD_FROM_YEAR}-{B.SOLD_FROM_MONTH:02d}) "
        f"let through {kept}"
    )
