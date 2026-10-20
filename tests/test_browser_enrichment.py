import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from models import ImportBatch, IngestJob, PropertyForSale, PortalFinding
from portals.browser_enrichment import Observation, SaveObservations, scope, queue, apply_observation, save


def setup(db):
    b=ImportBatch(batch_type='for_sale',region='Auckland',filename='new.csv',status='staged',is_active=False)
    db.add(b);db.flush()
    p=PropertyForSale(import_batch_id=b.id,address='2/24 Example Road',suburb='Westgate',
                      floor_area_m2=None,land_area_m2=405,cv_numeric=1000000,
                      property_type='House',sale_method='fixed price')
    db.add(p);db.flush()
    core=IngestJob(batch_type='for_sale',batch_id=b.id,filename=f'enrich (batch {b.id})',
                   status='failed',stage='enrich',result_json=json.dumps({'ids':[p.id]}))
    db.add(core);db.commit()
    return b,p,core


def observation(p,**changes):
    value=dict(property_id=p.id,address=p.address,suburb=p.suburb,
        source_url='https://www.trademe.co.nz/a/property/residential/sale/auckland/westgate/listing/123456?tracking=discard',
        observed_at=datetime.now(timezone.utc),evidence=f'{p.address}, {p.suburb}\nHouse\nFloor area 155m²\nLand area 405m²\nCV $1,000,000',
        facts={'floor_area_m2':{'value':155,'quote':'Floor area 155m²'}})
    value.update(changes)
    return Observation(**value)


def price(db,pid):
    db.get(PropertyForSale,pid).fair_value=1230000
    db.commit()


def test_saved_exact_scope_and_checkpoint(db_session):
    b,p,core=setup(db_session)
    other=PropertyForSale(import_batch_id=b.id,address='24 Example Road',suburb='Westgate')
    db_session.add(other);db_session.commit()
    q=queue(b.id,10,None,db_session)
    assert q['scope_total']==1 and [r['id'] for r in q['listings']]==[p.id]
    out=apply_observation(db_session,p,observation(p),None,price)
    assert out['filled']==1 and p.floor_area_m2==155 and p.land_area_m2==405
    assert p.fair_value==1230000 and queue(b.id,10,None,db_session)['remaining']==0
    out=apply_observation(db_session,p,observation(p),None,price)
    assert out['status']=='already_saved'
    findings=db_session.query(PortalFinding).all()
    assert len(findings)==2
    raw=json.loads(next(f for f in findings if f.field=='floor_area_m2').extra_json)
    assert raw['quote']=='Floor area 155m²' and '?' not in raw['url'] and raw['observed_at']


@pytest.mark.parametrize('changes',[
    {'address':'1/24 Example Road'}, {'suburb':'Henderson'},
    {'address':'24 Example Road'}, {'address':'2&3/24 Example Road'},
    {'evidence':'Floor area 155m² no subject address here'},
    {'observed_at':datetime.now()},
    {'observed_at':datetime.now(timezone.utc)-timedelta(days=2)},
    {'observed_at':datetime.now(timezone.utc)+timedelta(hours=1)},
    {'facts':{'floor_area_m2':{'value':999,'quote':'Floor area 155m²'}}},
    {'facts':{'floor_area_m2':{'value':155,'quote':'invented quote'}}},
    {'facts':{'land_area_m2':{'value':155,'quote':'Floor area 155m²'}}},
    {'facts':{'fair_value':{'value':155,'quote':'Floor area 155m²'}}},
    {'facts':{'floor_area_m2':{'value':float('inf'),'quote':'Floor area 155m²'}}},
    {'property_type':'Section'},
])
def test_bad_observation_cannot_change_facts(db_session,changes):
    b,p,_=setup(db_session)
    with pytest.raises(HTTPException):apply_observation(db_session,p,observation(p,**changes),None,price)
    assert p.floor_area_m2 is None and db_session.query(PortalFinding).count()==0


@pytest.mark.parametrize('url',[
    'https://www.trademe.co.nz.evil.test/a/property/residential/sale/listing/123',
    'http://www.trademe.co.nz/a/property/residential/sale/listing/123',
    'https://user:secret@www.trademe.co.nz/a/property/residential/sale/listing/123',
    'https://www.trademe.co.nz/a/motors/cars/toyota/listing/123',
    'https://127.0.0.1/a/property/residential/sale/listing/123',
])
def test_only_property_source_links(url):
    with pytest.raises(ValidationError):observation(SimpleNamespace(id=1,address='24 Road',suburb='Westgate'),source_url=url)


def test_source_conflicts_never_overwrite_and_remain_held(db_session):
    from release import _hold_reason,hold_flagged_rows
    b,p,_=setup(db_session)
    item=observation(p,property_type='Section',evidence=f'{p.address}, {p.suburb}\nSection\nFloor area 155m²')
    out=apply_observation(db_session,p,item,None,lambda *_:pytest.fail('must not price conflicting evidence'))
    assert out['status']=='review' and p.floor_area_m2 is None and p.property_type=='House'
    assert _hold_reason(p).startswith('Source review:')
    hold_flagged_rows(db_session,b.id)
    assert p.is_held and p.hold_reason.startswith('Source review:')


def test_existing_values_are_not_replaced(db_session):
    b,p,_=setup(db_session)
    item=observation(p,evidence=f'{p.address}, {p.suburb}\nLand area 155m²',
                     facts={'land_area_m2':{'value':155,'quote':'Land area 155m²'}})
    out=apply_observation(db_session,p,item,None,price)
    assert out['conflicts']==['land_area_m2'] and p.land_area_m2==405


@pytest.mark.parametrize('state,active',[('published',True),('archived',False),('staged',True)])
def test_live_and_archived_batches_rejected(db_session,state,active):
    b,_,_=setup(db_session);b.status=state;b.is_active=active;db_session.commit()
    with pytest.raises(HTTPException):scope(db_session,b.id)


def test_absent_scope_never_expands_to_whole_batch(db_session):
    b,p,core=setup(db_session);core.result_json='{}';db_session.commit()
    with pytest.raises(HTTPException):scope(db_session,b.id)


def test_out_of_scope_and_cross_batch_rejected(db_session):
    b,p,core=setup(db_session)
    core.result_json=json.dumps({'ids':[]});db_session.commit()
    with pytest.raises(HTTPException):save(SaveObservations(batch_id=b.id,observations=[observation(p)]),SimpleNamespace(id=None),db_session)
    assert p.floor_area_m2 is None


def test_pricing_failure_rolls_back_fact_and_checkpoint(db_session,monkeypatch):
    import reprice
    b,p,_=setup(db_session)
    def fail(*a,**k):raise ValueError('synthetic pricing failure')
    monkeypatch.setattr(reprice,'reprice_one',fail)
    with pytest.raises(HTTPException) as e:save(SaveObservations(batch_id=b.id,observations=[observation(p)]),SimpleNamespace(id=None),db_session)
    assert e.value.status_code==503
    db_session.refresh(p)
    assert p.floor_area_m2 is None and db_session.query(PortalFinding).count()==0


def test_routes_admin_only():
    from routers.release import router
    app=FastAPI();app.include_router(router)
    with TestClient(app) as c:
        assert c.get('/api/admin/release/browser-enrichment?batch_id=1').status_code==401
        assert c.post('/api/admin/release/browser-enrichment',json={'batch_id':1,'observations':[]}).status_code==401


def test_browser_fact_runs_real_pricing_without_publishing(db_session):
    from ingest import ingest_sold,ingest_for_sale
    from tests.test_the_link_check_gets_round_the_book import _sold,_for_sale
    sold=_sold()
    ingest_sold(db_session,sold,'sold.csv')
    result=ingest_for_sale(db_session,_for_sale(['24']),sold,'new.csv',publish=False,fill_missing=False)
    p=db_session.query(PropertyForSale).filter_by(import_batch_id=result.batch_id).one()
    p.floor_area_m2=None
    db_session.add(IngestJob(batch_type='for_sale',batch_id=result.batch_id,
        filename=f'enrich (batch {result.batch_id})',status='failed',stage='enrich',
        result_json=json.dumps({'ids':[p.id]})))
    db_session.commit()
    out=save(SaveObservations(batch_id=result.batch_id,observations=[observation(p)]),SimpleNamespace(id=None),db_session)
    db_session.refresh(p)
    assert out['filled']==1 and p.floor_area_m2==155 and p.fair_value is not None
    batch=db_session.get(ImportBatch,result.batch_id)
    assert batch.status=='staged' and not batch.is_active


def test_real_new_homes_property_link_accepted():
    p=SimpleNamespace(id=1,address='8 Umar Court',suburb='Warkworth')
    item=observation(p,source_url='https://www.trademe.co.nz/a/property/new-homes/new-house/auckland/rodney/warkworth/listing/6182378066?tracking=discard')
    assert item.source_url.endswith('/listing/6182378066')


def test_queue_prioritizes_missing_floor_before_history_only(db_session):
    b,p,core=setup(db_session)
    p.floor_area_m2=155
    gap=PropertyForSale(import_batch_id=b.id,address='25 Example Road',suburb='Westgate',floor_area_m2=None)
    db_session.add(gap);db_session.flush()
    core.result_json=json.dumps({'ids':[p.id,gap.id]});db_session.commit()
    assert queue(b.id,1,None,db_session)['listings'][0]['id']==gap.id


def pending_correction(db):
    b,p,core=setup(db)
    p.property_type='Residential - Vacant';db.commit()
    apply_observation(db,p,observation(p,property_type='House'),None,price)
    marker=db.query(PortalFinding).filter_by(field='browser_observation').one()
    return b,p,core,marker


def test_review_correction_preserves_before_and_prices_once(db_session):
    from portals.browser_enrichment import apply_reviewed_correction
    b,p,_,marker=pending_correction(db_session)
    calls=[]
    def reprice(db,pid):
        calls.append(pid)
        prop=db.get(PropertyForSale,pid)
        assert prop.floor_area_m2==155 and prop.property_type=='House'
        assert prop.hold_reason is None
        prop.fair_value=1250000
    out=apply_reviewed_correction(db_session,p,marker,None,reprice)
    assert out['changed']==['floor_area_m2','property_type'] and out['fair_value']==1250000
    assert marker.status=='approved'
    assert json.loads(marker.extra_json)['previous']['property_type']=='Residential - Vacant'
    assert queue(b.id,10,None,db_session)['source_review_count']==0
    assert apply_reviewed_correction(db_session,p,marker,None,reprice)['status']=='already_applied'
    assert calls==[p.id] and not b.is_active and b.status=='staged'
    fields=db_session.query(PortalFinding).filter(PortalFinding.field!='browser_observation').all()
    assert {f.field for f in fields}=={'floor_area_m2','property_type'}
    assert all(json.loads(f.extra_json)['reviewed_observation_id']==marker.id for f in fields)


@pytest.mark.parametrize('field,value',[('property_type','Townhouse'),('floor_area_m2',190),('asking_price',900000),('hold_reason','Held by admin')])
def test_review_rejects_concurrent_changes(db_session,field,value):
    from portals.browser_enrichment import apply_reviewed_correction
    b,p,_,marker=pending_correction(db_session)
    setattr(p,field,value);db_session.commit()
    with pytest.raises(HTTPException) as e:
        apply_reviewed_correction(db_session,p,marker,None,lambda *_:pytest.fail('must not reprice'))
    assert e.value.status_code==409 and marker.status=='review'


def test_review_price_failure_rolls_back_every_change(db_session,monkeypatch):
    from portals.browser_enrichment import review_correction,ReviewCorrection
    import reprice
    b,p,_,marker=pending_correction(db_session)
    def fail(*a,**kw):raise ValueError('no sold data')
    monkeypatch.setattr(reprice,'reprice_one',fail)
    with pytest.raises(HTTPException) as e:
        review_correction(ReviewCorrection(batch_id=b.id,finding_id=marker.id),SimpleNamespace(id=None),db_session)
    assert e.value.status_code==503
    db_session.refresh(p);db_session.refresh(marker)
    assert p.floor_area_m2 is None and p.property_type=='Residential - Vacant' and marker.status=='review'
    assert db_session.query(PortalFinding).count()==1


def test_review_scope_and_live_guards(db_session):
    from portals.browser_enrichment import review_correction,ReviewCorrection
    b,p,core,marker=pending_correction(db_session)
    core.result_json=json.dumps({'ids':[]});db_session.commit()
    with pytest.raises(HTTPException) as e:
        review_correction(ReviewCorrection(batch_id=b.id,finding_id=marker.id),SimpleNamespace(id=None),db_session)
    assert e.value.status_code==404
    b.is_active=True;db_session.commit()
    with pytest.raises(HTTPException) as e:
        review_correction(ReviewCorrection(batch_id=b.id,finding_id=marker.id),SimpleNamespace(id=None),db_session)
    assert e.value.status_code==409


def test_review_route_requires_admin():
    from routers.release import router
    app=FastAPI();app.include_router(router)
    with TestClient(app) as c:
        assert c.post('/api/admin/release/browser-enrichment/review',json={'batch_id':1,'finding_id':1}).status_code==401


def test_review_correction_runs_actual_engine(db_session):
    from ingest import ingest_sold,ingest_for_sale
    from tests.test_the_link_check_gets_round_the_book import _sold,_for_sale
    from portals.browser_enrichment import review_correction,ReviewCorrection
    sold=_sold();ingest_sold(db_session,sold,'sold.csv')
    result=ingest_for_sale(db_session,_for_sale(['24']),sold,'new.csv',publish=False,fill_missing=False)
    p=db_session.query(PropertyForSale).filter_by(import_batch_id=result.batch_id).one()
    p.floor_area_m2=None;p.property_type='Residential - Vacant'
    db_session.add(IngestJob(batch_type='for_sale',batch_id=result.batch_id,filename=f'enrich (batch {result.batch_id})',
        status='failed',stage='enrich',result_json=json.dumps({'ids':[p.id]})));db_session.commit()
    apply_observation(db_session,p,observation(p,property_type='House'),None,lambda *_:pytest.fail('review first'))
    marker=db_session.query(PortalFinding).filter_by(field='browser_observation').one()
    out=review_correction(ReviewCorrection(batch_id=result.batch_id,finding_id=marker.id),SimpleNamespace(id=None),db_session)
    assert p.property_type=='House' and p.floor_area_m2==155 and p.fair_value is not None
    assert out['status']=='applied' and marker.status=='approved'
