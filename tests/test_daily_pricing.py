import pytest
from test_reprice_memory import _batches
from models import PropertyForSale, ImportBatch
from portals.daily_pricing import reprice_live
from reprice import _sold_df


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
