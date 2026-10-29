from datetime import datetime, timezone
import json
import pytest

from asking_history import record_batch, bootstrap, evidence, attach
from models import AskingPriceObservation as O, ImportBatch, PropertyForSale as P


def make(db, day, price=1_000_000, **kwargs):
    status = kwargs.pop('status', 'published')
    b = ImportBatch(batch_type='for_sale', region='Auckland', filename='synthetic.csv',
        status=status, is_active=status == 'published',
        created_at=datetime(2026, 10, day, 14, tzinfo=timezone.utc))
    db.add(b); db.flush()
    values = dict(address='2/14 Example Road', suburb='Riverhead', region='Auckland',
        slug_id='source-1', asking_price=price, asking_basis='advertised',
        cv_numeric=850000, sale_method='fixed', url='https://example.com/2-14', is_held=False,
        property_type='House', floor_area_m2=150, land_area_m2=400, fair_value=1200000)
    values.update(kwargs)
    p = P(import_batch_id=b.id, **values)
    db.add(p); db.flush()
    return b, p


def test_reduction_retained_across_unchanged_uploads_and_different_source_ids(db_session):
    db=db_session
    for day, price, slug in [(1,1000000,'old'), (8,950000,'new'), (15,950000,'new')]:
        b,p=make(db,day,price,slug_id=slug)
        assert record_batch(db,b.id)==1
        assert record_batch(db,b.id)==0
    db.commit()
    data=evidence(db,p)
    assert len(data['observations'])==3
    change=data['latest_change']
    assert change['change_dollars']==-50000 and change['change_pct']==-5
    assert change['recorded_at'].startswith('2026-10-08')
    assert data['observations'][1]['recorded_date']=='2026-10-09'  # NZDT


@pytest.mark.parametrize('values', [
    {'status':'staged'}, {'status':'preview'}, {'status':'quarantined'},
    {'is_held':True}, {'asking_basis':'last advertised, less 3%'},
    {'asking_basis':'enquiries over'}, {'sale_method':'auction'},
    {'sale_method':'By negotiation'}, {'cv_numeric':1000000},
    {'asking_price':float('inf')}, {'asking_price':100}, {'url':None},
    {'address':'Example Road'}, {'address':'2 & 4 Example Road'},
])
def test_private_held_derived_placeholder_or_ambiguous_is_not_recorded(db_session, values):
    b,p=make(db_session,1,**values)
    assert record_batch(db_session,b.id)==0
    assert db_session.query(O).count()==0


def test_exact_units_and_suburbs_do_not_merge_even_with_reused_source_id(db_session):
    db=db_session
    b,p=make(db,1);record_batch(db,b.id)
    b,p=make(db,8,900000,address='3/14 Example Road');record_batch(db,b.id)
    assert evidence(db,p)['latest_change'] is None
    assert len(evidence(db,p)['observations'])==1
    b,p=make(db,15,900000,suburb='Kumeu');record_batch(db,b.id)
    assert len(evidence(db,p)['observations'])==1


def test_duplicate_exact_identities_are_not_recorded(db_session):
    db=db_session;b,p=make(db,1)
    db.add(P(import_batch_id=b.id,address=p.address,suburb=p.suburb,region=p.region,
        asking_price=900000,asking_basis='advertised',url=p.url,is_held=False));db.flush()
    assert record_batch(db,b.id)==0


def test_baseline_survives_original_upload_deletion_without_inventing_history(db_session):
    db=db_session;b,p=make(db,1);db.commit()
    assert bootstrap(db)==1;db.commit()
    assert evidence(db,p)['latest_change'] is None
    db.delete(p);db.flush();db.delete(b);db.commit()
    assert db.query(O).count()==1
    b,p=make(db,8,950000);record_batch(db,b.id);db.commit()
    assert evidence(db,p)['latest_change']['change_dollars']==-50000


def test_publication_is_atomic_and_rollback_does_not_leave_price_evidence(db_session):
    from release import publish_release
    db=db_session;b,p=make(db,1);db.commit()
    draft,q=make(db,8,950000,status='staged');db.commit()
    assert record_batch(db,draft.id)==0
    publish_release(db,commit=False)
    assert evidence(db,q)['latest_change']['change_dollars']==-50000
    db.rollback()
    assert db.get(ImportBatch,draft.id).status=='staged'
    assert db.query(O).count()==0
    publish_release(db)
    assert db.query(O).count()==2


def test_large_differences_remain_evidence_but_are_not_promoted(db_session):
    db=db_session;b,p=make(db,1);record_batch(db,b.id)
    b,p=make(db,8,200000);record_batch(db,b.id)
    assert len(evidence(db,p)['observations'])==2
    assert evidence(db,p)['latest_change'] is None


def test_property_api_and_ollie_share_exact_retained_evidence(db_session, monkeypatch):
    from routers.properties import get_for_sale, property_history
    import assistant.tools as tools
    from contextlib import contextmanager
    db=db_session;b,p=make(db,1);record_batch(db,b.id);b.is_active=False;b.status='archived'
    b,p=make(db,8,950000);record_batch(db,b.id);db.commit()
    result=get_for_sale(p.id,db)
    assert result.asking_price_change['change_pct']==-5
    history=property_history(p.id,db)
    assert len(history.asking_observations)==2
    @contextmanager
    def session():
        yield db
    monkeypatch.setattr(tools,'SessionLocal',session)
    data=json.loads(tools.get_property(p.id))
    assert data['advertised_price_history']['latest_change']==result.asking_price_change
    p.is_held=True;db.commit()
    assert 'not currently available' in tools.get_property(p.id)


def test_withdrawn_price_or_unpublished_edit_cannot_claim_a_new_price_drop(db_session):
    db=db_session;b,p=make(db,1);record_batch(db,b.id)
    b,p=make(db,8,950000);record_batch(db,b.id)
    p.asking_price=900000
    attach(db,[p]);assert p.asking_price_change is None
    p.asking_price=950000;p.asking_basis='last advertised, less 3%'
    attach(db,[p]);assert p.asking_price_change is None
