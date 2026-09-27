from datetime import datetime, timezone
import pytest
from models import ImportBatch, PortalListing, PropertyForSale
from portals import review_flow as flow

def batch(db,status='published',active=True):
    b=ImportBatch(batch_type='for_sale',region='Auckland',filename='test',status=status,is_active=active)
    db.add(b);db.flush();return b

def draft(db,b,address='1/2 Test Road',held=False):
    p=PropertyForSale(import_batch_id=b.id,address=address,suburb='Test',fair_value=900000,is_held=held)
    db.add(p);db.flush()
    r=PortalListing(source='oneroof',kind='for_sale',status='priced',address=address,suburb='Test',address_key=address,property_id=p.id)
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
    p=flow.price_one(db,r,b.id,None,None,None);db.commit()
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
