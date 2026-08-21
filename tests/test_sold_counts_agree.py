"""Three places count the same sales. They have to agree.

One screen showed all three disagreeing at once:

    picker   "Mount Eden — 111 sold"
    panel    "231 sold in 2026"
    map      "Sold sale · 111"

Sold history ACCUMULATES — each upload is a delivery, not the dataset — and the
picker and the map were both counting the newest batch only, while the panel
read every live batch. So the same suburb had two different sizes on one page,
and neither number told the reader which one to believe.

For-sale is the opposite case and stays on the active batch: that one really is
a weekly snapshot, and "live" means live now.
"""
from __future__ import annotations

from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold
from app.routers.properties import map_points, suburb_stats, suburbs


def _sold_batch(db, n, *, year, region="Auckland", suburb="Mount Eden", start=0):
    batch = ImportBatch(batch_type=BatchType.SOLD.value, region=region,
                        filename=f"sold-{year}.csv", rows_total=n, is_active=True,
                        status="published")
    db.add(batch); db.flush()
    for i in range(n):
        db.add(PropertySold(slug_id=f"s{start + i}", address=f"{start + i} Sold St",
                            suburb=suburb, region=region,
                            sale_price=1_500_000 + i * 1_000,
                            sold_date=f"{year}-06-15", beds=3, baths=1,
                            floor_area_m2=140, land_area_m2=500,
                            latitude=-36.87, longitude=174.76,
                            property_type="House", import_batch_id=batch.id))
    db.commit()
    return batch


def test_the_picker_counts_every_delivery_not_just_the_last(db_session):
    db = db_session
    # Two deliveries of the same suburb — which is what "sold data accumulates"
    # means in practice: last week's file, then this week's.
    _sold_batch(db, 60, year=2025, start=0)
    _sold_batch(db, 51, year=2026, start=100)

    opts = suburbs(region="Auckland", dataset="sold", district=None, db=db)
    mount_eden = next(o for o in opts if o.suburb == "Mount Eden")
    assert mount_eden.sold == 111, (
        f"the picker counted {mount_eden.sold}, not the 111 sales on file"
    )


def test_the_picker_and_the_panel_report_the_same_suburb(db_session):
    """The two numbers a reader sees side by side."""
    db = db_session
    _sold_batch(db, 60, year=2025, start=0)
    _sold_batch(db, 51, year=2026, start=100)

    opts = suburbs(region="Auckland", dataset="sold", district=None, db=db)
    picker = next(o for o in opts if o.suburb == "Mount Eden").sold

    # The panel, across every year rather than one, is the comparable figure.
    stats = suburb_stats(suburb="Mount Eden", region="Auckland",
                         from_year=2025, to_year=2026, ptype=None, db=db)
    assert picker == stats.sold_count == 111, (
        f"picker={picker} panel={stats.sold_count} — the same suburb, two sizes"
    )


def test_the_map_plots_every_delivery_too(db_session):
    db = db_session
    _sold_batch(db, 60, year=2025, start=0)
    _sold_batch(db, 51, year=2026, start=100)

    res = map_points(region="Auckland", dataset="sold", suburb="Mount Eden",
                         district=None, min_beds=None, min_price=None,
                         max_price=None, search=None, limit=20000, db=db)
    assert res.count == 111, f"the map plotted {res.count} of 111 sales"


def test_live_listings_still_mean_live(db_session):
    """The for-sale side must NOT accumulate: an old week is not on the market."""
    db = db_session
    old = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                      filename="last-week.csv", rows_total=5, is_active=False,
                      status="published")
    new = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                      filename="this-week.csv", rows_total=3, is_active=True,
                      status="published")
    db.add_all([old, new]); db.flush()
    for i in range(5):
        db.add(PropertyForSale(import_batch_id=old.id, region="Auckland",
                               suburb="Mount Eden", address=f"{i} Old Rd",
                               asking_price=1_000_000, floor_area_m2=120,
                               property_type="House", is_held=False))
    for i in range(3):
        db.add(PropertyForSale(import_batch_id=new.id, region="Auckland",
                               suburb="Mount Eden", address=f"{i} New Rd",
                               asking_price=1_000_000, floor_area_m2=120,
                               property_type="House", is_held=False))
    db.commit()

    opts = suburbs(region="Auckland", dataset="for_sale", district=None, db=db)
    mount_eden = next(o for o in opts if o.suburb == "Mount Eden")
    assert mount_eden.live == 3, (
        f"{mount_eden.live} listings called live — last week's are not on the market"
    )
