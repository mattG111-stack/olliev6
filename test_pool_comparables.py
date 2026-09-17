"""The comparables the reader is shown have to obey the same rule as the model.

The valuation now compares a house with a pool against sales of houses with
pools. If the panel underneath it still lists a mixed set, the numbers on screen
do not add up to the number above them — and the panel is the one thing that
lets a reader check the valuation for themselves.
"""
from __future__ import annotations

from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold
from app.routers.properties import property_comparables


def _world(db, *, subject_pool, pool_sales, plain_sales):
    sold_batch = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                             filename="sold.csv", rows_total=0, is_active=True,
                             status="published")
    live = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                       filename="live.csv", rows_total=1, is_active=True,
                       status="published")
    db.add_all([sold_batch, live]); db.flush()

    n = 0
    for has_pool, count in ((True, pool_sales), (False, plain_sales)):
        for _ in range(count):
            n += 1
            db.add(PropertySold(
                slug_id=f"s{n}", address=f"{n} Comp Street", suburb="Mount Eden",
                region="Auckland", property_type="House", beds=4, baths=2,
                floor_area_m2=200, land_area_m2=600,
                sale_price=1_400_000 + n * 1_000, cv_numeric=1_400_000,
                sold_date="2026-05-15", type_of_title="Freehold",
                has_swimming_pool=has_pool, import_batch_id=sold_batch.id))

    subject = PropertyForSale(
        import_batch_id=live.id, region="Auckland", suburb="Mount Eden",
        address="1 Subject Street", asking_price=1_500_000, cv_numeric=1_400_000,
        beds=4, baths=2, floor_area_m2=200, land_area_m2=600,
        property_type="House", type_of_title="Freehold",
        has_swimming_pool=subject_pool, is_held=False)
    db.add(subject); db.commit(); db.refresh(subject)
    return subject


def test_a_pool_house_is_shown_pool_comps(db_session):
    subject = _world(db_session, subject_pool=True, pool_sales=6, plain_sales=6)
    res = property_comparables(property_id=subject.id, db=db_session)
    assert res.comps, "no comparables at all"
    assert all(c.has_pool for c in res.comps), (
        "a house with a pool was shown sales of houses without one"
    )
    assert res.matched_using.get("pool") == "with a pool"


def test_a_house_without_one_is_shown_houses_without_one(db_session):
    subject = _world(db_session, subject_pool=False, pool_sales=6, plain_sales=6)
    res = property_comparables(property_id=subject.id, db=db_session)
    assert res.comps and not any(c.has_pool for c in res.comps)
    assert res.matched_using.get("pool") == "no pool"


def test_too_few_matching_sales_keeps_them_all_and_says_so(db_session):
    """Better a mixed set the reader can see than three cherry-picked sales."""
    subject = _world(db_session, subject_pool=True, pool_sales=1, plain_sales=8)
    res = property_comparables(property_id=subject.id, db=db_session)
    assert len(res.comps) > 1
    assert res.matched_using.get("pool") == "mixed"


def test_a_suburb_with_no_pools_at_all_says_nothing_about_them(db_session):
    subject = _world(db_session, subject_pool=False, pool_sales=0, plain_sales=8)
    res = property_comparables(property_id=subject.id, db=db_session)
    assert "pool" not in res.matched_using
    assert all(c.has_pool is False for c in res.comps)


def test_every_comp_says_whether_it_has_one(db_session):
    """So the panel can mark them rather than the reader having to guess."""
    subject = _world(db_session, subject_pool=True, pool_sales=1, plain_sales=8)
    res = property_comparables(property_id=subject.id, db=db_session)
    assert any(c.has_pool for c in res.comps)
    assert any(not c.has_pool for c in res.comps)
