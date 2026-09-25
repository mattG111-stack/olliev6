import pytest
from models import PropertyForSale, PropertySold, ImportBatch, BatchType
from portals.daily_pricing import reprice_live
from reprice import _sold_df


def _batches(db, n_listings: int, n_sold: int = 12):
    fs = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                     filename="week.xlsx", is_active=True, status="staged")
    sold = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                       filename="sold.xlsx", is_active=True, status="published")
    db.add_all([fs, sold])
    db.flush()
    for i in range(n_listings):
        db.add(PropertyForSale(
            import_batch_id=fs.id, address=f"{i} Test Road", suburb="Papakura",
            district="Papakura", property_type="House", beds=3, baths=1,
            floor_area_m2=140.0 + i, land_area_m2=600.0, cv_numeric=900_000,
            land_value_numeric=500_000, improvement_value_numeric=400_000,
            asking_price=950_000, type_of_title="Freehold"))
    for i in range(n_sold):
        db.add(PropertySold(
            import_batch_id=sold.id, address=f"{i} Sold Street", suburb="Papakura",
            district="Papakura", property_type="House", beds=3, baths=1,
            floor_area_m2=140.0 + i, land_area_m2=600.0, cv_numeric=880_000,
            sale_price=900_000 + i * 1000, sold_date="2026-06-01",
            type_of_title="Freehold"))
    db.commit()
    return fs, sold



def test_daily_price_failure_rolls_back_previously_flushed_chunks(db_session,monkeypatch):
    import reprice
    fs,_=_batches(db_session,4)
    fs.status='published';db_session.commit();bid=fs.id
    original=reprice._apply_outputs;calls=[]
    def fail(record,row):
        calls.append(record.id)
        if len(calls)==3:raise RuntimeError('synthetic mid-run failure')
        original(record,row)
    monkeypatch.setattr(reprice,'_apply_outputs',fail)
    with pytest.raises(RuntimeError):reprice_live(db_session,bid,chunk=2)
    assert len(calls)==3
    assert all(p.fair_value is None for p in db_session.query(PropertyForSale).all())


def test_complete_daily_price_commits_all_rows(db_session):
    fs,_=_batches(db_session,4)
    fs.status='published';db_session.commit();bid=fs.id
    result=reprice_live(db_session,bid,chunk=2)
    assert result.committed and result.rows==4
    assert db_session.query(PropertyForSale).filter(PropertyForSale.fair_value.isnot(None)).count()==4


def test_unpublished_sold_data_excluded_only_in_new_daily_path(db_session):
    fs,sold=_batches(db_session,1)
    sold.status='preview';db_session.commit()
    assert _sold_df(db_session,'Auckland') is not None
    assert _sold_df(db_session,'Auckland',published_only=True) is None
    fs.status='published';db_session.commit()
    with pytest.raises(ValueError,match='no sold batch'):reprice_live(db_session,fs.id)


def test_daily_price_cannot_publish_a_staged_listing_batch(db_session):
    fs,_=_batches(db_session,1)
    with pytest.raises(ValueError,match='active published'):reprice_live(db_session,fs.id)


def test_later_dispute_excludes_only_matching_dated_sale_from_daily_comps(db_session):
    from models import PortalListing
    _,sold=_batches(db_session,1)
    db_session.add(PropertySold(import_batch_id=sold.id,address='0 Sold Street',
        suburb='Papakura',sale_price=800000,sold_date='2025-06-01'))
    dispute=PortalListing(source='homes',kind='sold',status='pending',
        address='0 Sold Street',suburb='Papakura',sold_date='06/01/2026',
        price_flag='Disclosed price conflicts with an existing sale')
    db_session.add(dispute);db_session.commit()
    assert len(_sold_df(db_session,'Auckland'))==13  # CSV semantics retained.
    daily=_sold_df(db_session,'Auckland',published_only=True)
    assert len(daily)==12
    matching=daily[daily.address=='0 Sold Street']
    assert list(matching.sold_date)==['2025-06-01']
    dispute.status='rejected';db_session.commit()
    assert len(_sold_df(db_session,'Auckland',published_only=True))==13
