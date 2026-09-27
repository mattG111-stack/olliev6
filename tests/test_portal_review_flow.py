from datetime import datetime, timezone
import pytest
from models import ImportBatch, PortalListing, PropertyForSale
from portals import review_flow as flow

def batch(db,status='published',active=True):
    b=ImportBatch(batch_type='for_sale',region='Auckland',filename='test',status=status,is_active=active)
    db.add(b);db.flush();return b

def draft(db,b,address='1/2 Test Road',held=False):
    import json
    facts=dict(floor_area_m2=100,land_area_m2=300,beds=3,baths=1,cv_numeric=900000,image_url='https://example.test/photo.jpg')
    p=PropertyForSale(import_batch_id=b.id,address=address,suburb='Test',fair_value=900000,is_held=held,confidence='high',comps_used=8,**facts)
    db.add(p);db.flush()
    r=PortalListing(source='oneroof',kind='for_sale',status='priced',address=address,suburb='Test',address_key=address,property_id=p.id,**facts,
        raw_json=json.dumps({'_apex_direct':True,'address':address,'suburb':'Test','scraped_at':datetime.now(timezone.utc).isoformat()}))
    db.add(r);db.commit();return r,p

def test_counts_midnight_dst_week_source_dedup(db_session):
    db=db_session;now=datetime(2026,9,27,11,30,tzinfo=timezone.utc)
    for source,kind,key,hour,minute in [('oneroof','for_sale','same',10,59),('homes','for_sale','same',11,10),('trademe','for_sale','new',11,1),('homes','sold','sold',11,2)]:
        db.add(PortalListing(source=source,kind=kind,status='pending',address_key=key,created_at=datetime(2026,9,27,hour,minute,tzinfo=timezone.utc)))
    db.commit();c=flow.download_counts(db,now)
    assert c['for_sale']=={'today':1,'week':1} and c['sold']=={'today':1,'week':1}
    assert c['week_starts']=='2026-09-28'

def test_removal_reversible_and_private(db_session):
    db=db_session;live=batch(db);b=batch(db,flow.DRAFT,False);r,p=draft(db,b)
    flow.remove(db,r.id,True);assert r.status=='removed'
    assert p.import_batch_id==b.id and not b.is_active and live.is_active
    flow.remove(db,r.id,False);assert r.status=='priced'
    assert flow.review(db)['rows'][0]['value']==900000

def test_live_gate(db_session,monkeypatch):
    db=db_session;batch(db);b=batch(db,flow.DRAFT,False);r,p=draft(db,b)
    monkeypatch.delenv('PORTAL_REVIEW_PUBLISH_ENABLED',raising=False)
    with pytest.raises(ValueError,match='validation'):flow.publish(db,[r.id],None)
    assert p.import_batch_id==b.id and r.status=='priced'

def test_publish_additive_idempotent(db_session,monkeypatch):
    db=db_session;live=batch(db);old=PropertyForSale(import_batch_id=live.id,address='9 Other Road',suburb='Test');db.add(old)
    b=batch(db,flow.DRAFT,False);r,p=draft(db,b)
    monkeypatch.setenv('PORTAL_REVIEW_PUBLISH_ENABLED','true')
    assert flow.publish(db,[r.id],None)==1
    assert p.import_batch_id==live.id and old.import_batch_id==live.id and live.is_active and not b.is_active
    with pytest.raises(ValueError):flow.publish(db,[r.id],None)

def test_invalid_selection_atomic(db_session,monkeypatch):
    db=db_session;batch(db);b=batch(db,flow.DRAFT,False);r,p=draft(db,b);r2,p2=draft(db,b,'2/2 Test Road',True)
    monkeypatch.setenv('PORTAL_REVIEW_PUBLISH_ENABLED','true')
    with pytest.raises(ValueError):flow.publish(db,[r.id,r2.id],None)
    db.rollback();assert p.import_batch_id==p2.import_batch_id==b.id

def test_single_job_private_batch(db_session):
    db=db_session;live=batch(db);db.commit();j,new=flow.start(db,[12,12,13],None)
    assert new and j.rows_total==2
    j2,new2=flow.start(db,[14],None);assert j2.id==j.id and not new2
    assert db.query(ImportBatch).filter_by(status=flow.DRAFT,is_active=False).count()==1 and live.is_active

def test_source_mapping_no_writes(db_session):
    from portals.listings import property_from_listing
    row=PortalListing(address='2/12 Test Road',suburb='Test',price_numeric=800000,land_area_m2=300,floor_area_m2=100,carspaces=2,kind='for_sale',source='homes',prior_sale_price=700000,prior_sale_date='2020-02-03')
    p=property_from_listing(row,99)
    assert p.address=='2/12 Test Road' and p.floor_area_m2==100 and p.land_area_m2==300
    assert p.cars==2 and p.valuation_last_sold_value==700000 and p.asking_price==800000
    assert p.id is None and db_session.query(PropertyForSale).count()==0

def test_price_worker_uses_published_sales_and_leaves_failures_pending(db_session,monkeypatch):
    import pandas as pd
    import reprice
    import ml.store
    from db import SessionLocal
    db=db_session
    r=PortalListing(source='oneroof',kind='for_sale',status='pending',address='2/9 Test Road',suburb='Test',address_key='2-9')
    r2=PortalListing(source='homes',kind='for_sale',status='pending',address='3/9 Test Road',suburb='Test',address_key='3-9')
    db.add_all([r,r2]);db.commit();ids=[r.id,r2.id];j,_=flow.start(db,ids,None);jid=j.id
    flags=[]
    def sales(db,region,*,published_only=False):
        flags.append(published_only);return pd.DataFrame([{'sale_price':800000}])
    monkeypatch.setattr(reprice,'_sold_df',sales)
    monkeypatch.setattr(reprice,'_rent_rates',lambda *a:None)
    monkeypatch.setattr(ml.store,'live_model',lambda *a:None)
    import pricing.comps
    monkeypatch.setattr(pricing.comps,'SoldDataset',lambda x:x)
    def price(db,row,bid,*args):
        if row.id==ids[1]:raise ValueError('No usable floor area')
        p=PropertyForSale(import_batch_id=bid,address=row.address,suburb=row.suburb,fair_value=800000)
        db.add(p);db.flush();row.property_id=p.id;row.status='priced'
    monkeypatch.setattr(flow,'price_one',price)
    flow.run(jid);db.expire_all()
    assert flags==[True]
    assert r.status=='priced' and r2.status=='pending'
    assert j.status=='completed' and j.rows_inserted==1 and j.rows_rejected==1 and j.progress_pct==100
    assert not db.get(ImportBatch,db.get(PropertyForSale,r.property_id).import_batch_id).is_active


def test_price_calculation_saves_only_to_draft(db_session,monkeypatch):
    import pandas as pd
    import reprice
    db=db_session;b=batch(db,flow.DRAFT,False)
    r=PortalListing(source='homes',kind='for_sale',status='pending',address='1/5 Test Road',address_key='1-5',suburb='Test',floor_area_m2=100,land_area_m2=300,price_numeric=700000)
    db.add(r);db.commit()
    def pipeline(df,*args,**kwargs):
        assert df.iloc[0]['address']=='1/5 Test Road'
        return pd.DataFrame([{'fair_value':800000}])
    monkeypatch.setattr(reprice,'run_pipeline',pipeline)
    from types import SimpleNamespace
    p=flow.price_one(db,r,b.id,SimpleNamespace(df=pd.DataFrame({"suburb":["Test"]})),None,None);db.commit()
    assert r.status=='priced' and p.import_batch_id==b.id and not b.is_active
    assert p.fair_value==800000

def test_public_property_routes_refuse_private_drafts(db_session):
    from starlette.requests import Request
    from fastapi import HTTPException
    from routers.properties import exclude_portal_drafts
    db=db_session;b=batch(db,flow.DRAFT,False);r,p=draft(db,b)
    request=Request({'type':'http','path_params':{'property_id':str(p.id)}})
    with pytest.raises(HTTPException) as exc:exclude_portal_drafts(request,db)
    assert exc.value.status_code==404
    b.status='published';db.commit();exclude_portal_drafts(request,db)

def test_real_pipeline_prices_private_draft_without_changing_live(db_session):
    from tests.test_daily_pricing import _batches
    from reprice import _sold_df, _rent_rates
    from pricing.comps import SoldDataset
    db=db_session;live,_=_batches(db,1);before=db.query(PropertyForSale).filter_by(import_batch_id=live.id).one()
    b=batch(db,flow.DRAFT,False)
    r=PortalListing(source='homes',kind='for_sale',status='pending',address='3/12 Test Road',address_key='3-12-test',suburb='Papakura',district='Papakura',property_type='House',beds=3,baths=1,floor_area_m2=140,land_area_m2=600,cv_numeric=900000,land_value_numeric=500000,improvement_value_numeric=400000,price_numeric=850000,type_of_title='Freehold')
    db.add(r);db.commit()
    p=flow.price_one(db,r,b.id,SoldDataset(_sold_df(db,'Auckland',published_only=True)),_rent_rates(db,'Auckland'),None);db.commit()
    assert p.fair_value is not None and p.fair_value>0 and r.status=='priced'
    assert before.fair_value is None and p.import_batch_id==b.id and not b.is_active


def test_live_history_excludes_unpublished_portal_prices(db_session):
    from routers.properties import property_history
    db=db_session;live=batch(db);p=PropertyForSale(import_batch_id=live.id,address='1/2 Test Road',suburb='Test')
    db.add(p);db.commit();b=batch(db,flow.DRAFT,False);draft(db,b)
    history=property_history(p.id,db)
    assert len(history.points)==1 and history.points[0].batch_id==live.id


def test_portal_case_normalization_and_draft_reprice_preserve_identity(db_session):
    from tests.test_daily_pricing import _batches
    from reprice import _sold_df, _rent_rates
    from pricing.comps import SoldDataset
    db=db_session;_batches(db,1);b=batch(db,flow.DRAFT,False)
    r=PortalListing(source='homes',kind='for_sale',status='pending',address='3/12 Test Road',address_key='3-12-test',suburb='PAPAKURA',district='PAPAKURA',property_type='House',beds=3,baths=1,floor_area_m2=140,land_area_m2=600,cv_numeric=900000,land_value_numeric=500000,improvement_value_numeric=400000,price_numeric=850000,type_of_title='Freehold')
    db.add(r);db.commit();dataset=SoldDataset(_sold_df(db,'Auckland',published_only=True));rent=_rent_rates(db,'Auckland')
    p=flow.price_one(db,r,b.id,dataset,rent,None);db.commit();pid=p.id;value=p.fair_value
    assert p.suburb==p.district=='Papakura' and r.suburb=='PAPAKURA' and p.address=='3/12 Test Road'
    p2=flow.price_one(db,r,b.id,dataset,rent,None);db.commit()
    assert p2.id==pid and p2.fair_value==value
    assert db.query(PropertyForSale).filter_by(import_batch_id=b.id).count()==1
    live=batch(db);p.import_batch_id=live.id;db.commit()
    with pytest.raises(ValueError,match='private'):flow.price_one(db,r,b.id,dataset,rent,None)


def test_repricing_refreshes_source_facts_without_duplicate_or_live_write(db_session,monkeypatch):
    import pandas as pd
    import reprice
    from types import SimpleNamespace
    db=db_session;live=batch(db);b=batch(db,flow.DRAFT,False)
    old=PropertyForSale(import_batch_id=live.id,address='9 Other Road',suburb='Test',fair_value=777000)
    db.add(old)
    r=PortalListing(source='homes',kind='for_sale',status='pending',address='1/5 Test Road',address_key='1-5',suburb='TEST',floor_area_m2=100,land_area_m2=300,price_numeric=700000,image_url='https://example.test/one.jpg')
    db.add(r);db.commit();observed=[]
    def pipeline(df,*args,**kwargs):
        observed.append(dict(df.iloc[0]))
        return pd.DataFrame([{'fair_value':float(df.iloc[0]['key_floor_area'])*8000}])
    monkeypatch.setattr(reprice,'run_pipeline',pipeline)
    dataset=SimpleNamespace(df=pd.DataFrame({'suburb':['Test']}))
    p=flow.price_one(db,r,b.id,dataset,None,None);db.commit();pid=p.id
    r.floor_area_m2=150;r.land_area_m2=400;r.baths=2
    r.prior_sale_price=650000;r.prior_sale_date='2022-02-03'
    r.image_url='https://example.test/new.jpg'
    r.image_urls='https://example.test/new.jpg\nhttps://example.test/two.jpg'
    db.commit()
    p=flow.price_one(db,r,b.id,dataset,None,None);db.commit()
    assert observed[-1]['key_floor_area']==150 and observed[-1]['key_land_area']==400
    assert p.id==pid and p.fair_value==1200000 and p.baths==2
    assert p.image_url==r.image_url and p.image_urls==r.image_urls
    assert p.valuation_last_sold_value==650000 and p.valuation_last_sold_date=='2022-02-03'
    assert p.suburb=='Test' and r.suburb=='TEST'
    assert db.query(PropertyForSale).filter_by(import_batch_id=b.id).count()==1
    assert old.fair_value==777000 and old.import_batch_id==live.id


@pytest.mark.parametrize('field,value', [('floor_area_m2',150), ('land_area_m2',400),
    ('beds',4), ('baths',2), ('carspaces',2), ('price_numeric',750000),
    ('address','2/2 Test Road'), ('prior_sale_price',600000), ('building_age','2023')])
def test_publish_rejects_source_changes_after_pricing(db_session,monkeypatch,field,value):
    db=db_session;live=batch(db);b=batch(db,flow.DRAFT,False);r,p=draft(db,b)
    r2,p2=draft(db,b,'3/2 Test Road')
    setattr(r,field,value);db.commit()
    monkeypatch.setenv('PORTAL_REVIEW_PUBLISH_ENABLED','true')
    reviewed=next(x for x in flow.review(db)['rows'] if x['id']==r.id)
    assert reviewed['held'] and 'run pricing again' in reviewed['reason']
    with pytest.raises(ValueError,match='run pricing again'):flow.publish(db,[r2.id,r.id],None)
    db.rollback()
    assert p.import_batch_id==p2.import_batch_id==b.id and r.status=='priced'


def test_source_case_normalization_does_not_block_publication(db_session,monkeypatch):
    db=db_session;live=batch(db);b=batch(db,flow.DRAFT,False);r,p=draft(db,b)
    r.suburb='TEST';db.commit()
    monkeypatch.setenv('PORTAL_REVIEW_PUBLISH_ENABLED','true')
    assert not flow.changed_source_fields(r,p)
    assert flow.publish(db,[r.id],None)==1


@pytest.mark.parametrize('defect', ['missing_floor','missing_baths','missing_photo','weak_comps',
    'low_confidence','outlier','nan_value','raw_conflict','wrong_unit','old_evidence',
    'future_evidence','invalid_json','legacy_evidence'])
def test_readiness_blocks_unsafe_release_and_explains_it(db_session,monkeypatch,defect):
    import json
    from datetime import timedelta
    db=db_session;live=batch(db);b=batch(db,flow.DRAFT,False);r,p=draft(db,b)
    good,goodp=draft(db,b,'3/2 Test Road');raw=json.loads(r.raw_json)
    if defect=='missing_floor': r.floor_area_m2=p.floor_area_m2=None
    elif defect=='missing_baths': r.baths=p.baths=None
    elif defect=='missing_photo': r.image_url=p.image_url=None
    elif defect=='weak_comps': p.comps_used=4
    elif defect=='low_confidence': p.confidence='low'
    elif defect=='outlier': p.fair_value=2000000
    elif defect=='nan_value': p.fair_value=float('nan')
    elif defect=='raw_conflict': raw['source_conflicts']={'floor_area_m2':[100,180]}
    elif defect=='wrong_unit': raw['address']='2 Test Road'
    elif defect=='old_evidence': raw['scraped_at']=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    elif defect=='future_evidence': raw['scraped_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    elif defect=='legacy_evidence': raw.pop('_apex_direct')
    r.raw_json='not json' if defect=='invalid_json' else json.dumps(raw)
    db.commit()
    reasons=flow.readiness(r,p)
    assert reasons and flow.readiness(good,goodp)==[]
    visible=next(x for x in flow.review(db)['rows'] if x['id']==r.id)
    assert visible['held'] and visible['readiness_reasons']==reasons
    monkeypatch.setenv('PORTAL_REVIEW_PUBLISH_ENABLED','true')
    with pytest.raises(ValueError): flow.publish(db,[good.id,r.id],None)
    db.rollback()
    assert p.import_batch_id==goodp.import_batch_id==b.id
    assert r.status==good.status=='priced'


def test_interrupted_pricing_resumes_checkpoint_without_repricing_saved_rows(db_session,monkeypatch):
    import json
    import pandas as pd
    import reprice, ml.store, pricing.comps
    db=db_session
    rows=[PortalListing(source='homes',kind='for_sale',status='pending',address=f'{i} Test Road') for i in (1,2,3)]
    db.add_all(rows);db.commit();ids=[r.id for r in rows]
    job,_=flow.start(db,ids,None);jid=job.id
    monkeypatch.setattr(reprice,'_sold_df',lambda *a,**k:pd.DataFrame([{'sale_price':800000}]))
    monkeypatch.setattr(reprice,'_rent_rates',lambda *a:None)
    monkeypatch.setattr(ml.store,'live_model',lambda *a:None)
    monkeypatch.setattr(pricing.comps,'SoldDataset',lambda x:x)
    calls=[]; crash=[True]
    def price(db,row,bid,*args):
        calls.append(row.id)
        if row.id==ids[2]:raise ValueError('Missing source data')
        p=PropertyForSale(import_batch_id=bid,address=row.address,fair_value=800000)
        db.add(p);db.flush()
        if row.id==ids[1] and crash[0]:raise KeyboardInterrupt('simulated process loss after flush')
        row.property_id=p.id;row.status='priced'
    monkeypatch.setattr(flow,'price_one',price)
    with pytest.raises(KeyboardInterrupt): flow.run(jid)
    db.expire_all()
    assert job.status=='running' and job.rows_inserted==1
    assert json.loads(job.result_json)['completed_ids']==[ids[0]]
    assert db.query(PropertyForSale).count()==1
    crash[0]=False;flow.resume_pending();db.expire_all()
    assert job.status=='completed' and job.rows_inserted==2 and job.rows_rejected==1
    assert json.loads(job.result_json)['completed_ids']==ids
    assert calls.count(ids[0])==1 and db.query(PropertyForSale).count()==2
    flow.run(jid);flow.resume_pending()
    assert calls==[ids[0],ids[1],ids[1],ids[2]]
    assert rows[2].status=='pending'


def test_postgres_job_lock_prevents_duplicate_runner(db_session,monkeypatch):
    from db import engine
    from sqlalchemy import text
    if engine.dialect.name!='postgresql':pytest.skip('PostgreSQL session-lock regression')
    calls=[];monkeypatch.setattr(flow,'_run',lambda db,jid:calls.append(jid))
    with engine.connect() as owner:
        owner.execute(text('SELECT pg_advisory_lock(792634902, 123)'));owner.commit()
        try:
            flow.run(123);assert calls==[]
        finally:
            owner.execute(text('SELECT pg_advisory_unlock(792634902, 123)'));owner.commit()
    flow.run(123);assert calls==[123]


def test_legacy_interrupted_job_releases_stuck_state_without_guessing_checkpoint(db_session):
    db=db_session;j,_=flow.start(db,[1,2],None)
    j.status='running';j.rows_inserted=1;db.commit()
    flow.run(j.id);db.expire_all()
    assert j.status=='failed' and 'Run pricing again' in j.error_message
    assert j.rows_inserted==1
    next_job,created=flow.start(db,[1,2],None)
    assert created and next_job.id!=j.id
