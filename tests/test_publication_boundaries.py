"""Real database regressions for approval, scope, recovery and comparison."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from models import ImportBatch, PropertyForSale, PropertySold, IngestJob, PortalFinding
from release_safety import working_copy, pricing_fingerprint, validate_release
from publication_scope import approved_sold_batch_ids


def batch(db, kind='for_sale', status='staged', active=False):
    b=ImportBatch(batch_type=kind, region='Auckland', filename='synthetic.csv',status=status,is_active=active)
    db.add(b);db.flush();return b


def listing(db,b,**values):
    args=dict(address='8 Example Road',suburb='Riverhead',region='Auckland',floor_area_m2=200,
        fair_value=1000000,property_type='House',is_held=False)
    args.update(values)
    p=PropertyForSale(import_batch_id=b.id,**args);db.add(p);db.flush();return p


def receipt(db,b):
    j=IngestJob(batch_type='for_sale',filename=f'price (batch {b.id})',batch_id=b.id,
        stage='done',status='completed',result_json=json.dumps({'review_fingerprint':pricing_fingerprint(db,b.id)}))
    db.add(j);db.commit();return j


def test_copy_is_private_and_retains_facts_images_and_provenance(db_session):
    db=db_session;live=batch(db,status='published',active=True)
    p=listing(db,live,image_urls='one\ntwo',pv_data='{"cv":123}',is_held=True,hold_reason='Source review: exact unit')
    db.add(PortalFinding(property_id=p.id,batch_id=live.id,source='hougarden',field='floor_area_m2',kind='fact',status='approved',value_num=200));db.commit()
    draft=working_copy(db,'Auckland');db.commit()
    assert draft.status=='staged' and not draft.is_active and live.is_active
    q=db.query(PropertyForSale).filter_by(import_batch_id=draft.id).one()
    assert q.id!=p.id and q.image_urls==p.image_urls and q.pv_data==p.pv_data and q.hold_reason==p.hold_reason
    assert db.query(PortalFinding).filter_by(property_id=q.id,batch_id=draft.id).count()==1
    assert working_copy(db,'Auckland').id==draft.id
    q.floor_area_m2=250;db.commit();assert p.floor_area_m2==200


def test_release_requires_completed_current_pricing_and_preserves_holds(db_session):
    db=db_session;b=batch(db);p=listing(db,b,is_held=True,hold_reason='Source review: conflicting area');db.commit()
    with pytest.raises(ValueError,match='Run pricing'):validate_release(db,'Auckland')
    receipt(db,b);validate_release(db,'Auckland');assert p.is_held
    p.floor_area_m2=201;db.commit()
    with pytest.raises(ValueError,match='changed'):validate_release(db,'Auckland')
    receipt(db,b)
    j=IngestJob(batch_type='for_sale',filename='enrich',batch_id=b.id,status='pending',stage='enrich');db.add(j);db.commit()
    with pytest.raises(ValueError,match='queued or running'):validate_release(db,'Auckland')


def test_route_cannot_publish_while_running_or_without_receipt(db_session):
    from routers.release import publish
    db=db_session;live=batch(db,status='published',active=True);b=batch(db);listing(db,b);db.commit()
    with pytest.raises(HTTPException) as err:publish('Auckland',SimpleNamespace(id=None),db)
    assert err.value.status_code==409
    db.refresh(live);db.refresh(b);assert live.is_active and b.status=='staged'


def test_invalid_valuation_is_excluded_even_with_completed_run(db_session):
    db=db_session;b=batch(db);p=listing(db,b,fair_value=None);db.commit();receipt(db,b)
    validate_release(db,'Auckland');assert p.is_held and 'valuation' in p.hold_reason


def test_customer_sales_and_generated_sql_ignore_unapproved_active_flags(db_session):
    from assistant.sql import validate, scope_live_listings
    db=db_session
    pub=batch(db,'sold','published',False)
    for status in ['staged','preview','failed','archived']:
        b=batch(db,'sold',status,True);db.add(PropertySold(import_batch_id=b.id,address='2 Private Road',sale_price=99999999))
    db.add(PropertySold(import_batch_id=pub.id,address='1 Public Road',sale_price=1000000));db.commit()
    assert approved_sold_batch_ids(db,'Auckland')==[pub.id]
    for query in ['SELECT count(*) AS n FROM properties_sold',
                  'WITH s AS (SELECT * FROM "properties_sold") SELECT count(*) AS n FROM s']:
        sql=scope_live_listings(validate(query),db.get_bind().dialect)
        assert db.execute(text(sql)).scalar()==1
    from assistant.sql import UnsafeQuery
    for query in ['SELECT * FROM properties_sold, users', 'SELECT * FROM "users"',
                  'WITH users AS (SELECT 1) SELECT * FROM public.users']:
        with pytest.raises(UnsafeQuery):validate(query)
    for query in ['SELECT * FROM public.properties_sold',
                  'WITH properties_sold AS (SELECT 1) SELECT * FROM properties_sold']:
        with pytest.raises(UnsafeQuery):scope_live_listings(validate(query))


def test_direct_property_lookup_cannot_reveal_private_or_held_ids(db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.properties import router
    from db import get_db
    db=db_session;draft=batch(db);p=listing(db,draft);db.commit()
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        for suffix in ['', '/cashflow', '/external-estimates', '/sold-comps']:
            assert client.get(f'/api/properties/{p.id}{suffix}').status_code==404
        draft.status='published';draft.is_active=True;p.is_held=True;db.commit()
        assert client.get(f'/api/properties/{p.id}').status_code==404
        p.is_held=False;db.commit()
        assert client.get(f'/api/properties/{p.id}').status_code==200


def test_generic_addresses_and_exact_units_are_not_merged(db_session):
    from dedupe_forsale import dedupe_forsale_batch
    db=db_session;b=batch(db)
    for a in ['地址保密','地址保密','Address withheld 1','Address withheld 1','1/8 Example Road','2/8 Example Road']:
        listing(db,b,address=a)
    db.commit();r=dedupe_forsale_batch(db,batch_id=b.id,dry_run=False,exclude_only=True)
    assert r.excluded==0 and db.query(PropertyForSale).count()==6


def test_requested_job_is_queued_and_recovered_once_without_losing_checkpoint(db_session,monkeypatch):
    import requested_stages as worker
    import staged_stages as stages
    db=db_session;b=batch(db);p=listing(db,b)
    job=IngestJob(batch_type='for_sale',batch_id=b.id,filename=f'enrich (batch {b.id})',stage='enrich',status='running',result_json=json.dumps({'ids':[p.id],'saved':42}));db.add(job);db.commit()
    worker.queue_stage(db,job.id,'enrich','Auckland')
    calls=[]
    def finish(jid,bid,region):
        calls.append((jid,bid,region))
        with worker.SessionLocal() as s:
            j=s.get(IngestJob,jid);assert json.loads(j.result_json)['saved']==42;j.status='completed';s.commit()
    monkeypatch.setattr(stages,'run_enrich_job',finish)
    worker.run_pending();worker.run_pending()
    assert calls==[(job.id,b.id,'Auckland')]
    db.refresh(job);assert json.loads(job.result_json)['worker_attempts']==1


def test_recovery_attempts_are_bounded_and_live_work_rejected(db_session,monkeypatch):
    import requested_stages as worker
    db=db_session;b=batch(db);j=IngestJob(batch_type='for_sale',batch_id=b.id,filename=f'price (batch {b.id})',stage='price',status='running',result_json=json.dumps({'requested_stage':{'version':1,'stage':'price','region':'Auckland'},'worker_attempts':3}));db.add(j);db.commit()
    worker.run_pending();db.refresh(j);assert j.status=='failed'
    b.status='published';b.is_active=True;db.commit()
    with pytest.raises(ValueError,match='private'):worker.queue_stage(db,j.id,'price','Auckland')


def test_hougarden_benchmark_requires_dated_source_and_does_not_change_values(db_session):
    from hougarden_benchmark import benchmark
    db=db_session;b=batch(db);p=listing(db,b,url='https://hougarden.com/property/example',third_party_valuation=900000,valuation_last_date='2026-01-01')
    listing(db,b,address='10 Example Road',url='https://homes.co.nz/example',third_party_valuation=1200000,valuation_last_date='2026-01-01')
    listing(db,b,address='12 Example Road',url='https://hougarden.com/property/12',third_party_valuation=1200000)
    listing(db,b,address='14 Example Road',url='https://hougarden.com.evil.test/property/14',third_party_valuation=1200000,valuation_last_date='2026-01-01')
    for prop,stamp in [(p,'2026-01-01'),(db.query(PropertyForSale).filter_by(address='12 Example Road').one(),None)]:
        db.add(PortalFinding(property_id=prop.id,batch_id=b.id,source='hougarden',field='import_record',kind='fact',status='approved',extra_json=json.dumps({'captured_at':stamp,'original':{'address':prop.address,'suburb':prop.suburb,'property_valuation_numeric':prop.third_party_valuation}})))
    db.commit();report=benchmark(db,b)
    assert report['matched_properties']==1
    assert report['median_difference_pct']==pytest.approx(100/9)
    assert report['excluded']['unverified_source']==2 and report['excluded']['undated_or_future']==1
    assert p.fair_value==1000000 and p.third_party_valuation==900000 and b.status=='staged'


def test_paused_enrichment_cannot_resume_into_published_data(db_session, monkeypatch):
    from portals import batch_hougarden
    db=db_session;b=batch(db,status='published',active=True);p=listing(db,b)
    j=IngestJob(batch_type='for_sale',filename=f'hougarden_enrich (batch {b.id})',batch_id=b.id,stage='hougarden_enrich',status='paused',result_json=json.dumps({'ids':[p.id],'index':1,'filled':4}))
    db.add(j);db.commit()
    monkeypatch.setattr(batch_hougarden.source,'apply',lambda *_:pytest.fail('Published records must not be enriched'))
    batch_hougarden.run_pending();db.refresh(j)
    assert j.status=='failed' and json.loads(j.result_json)['filled']==4


def test_paused_source_prevents_publication_until_explicitly_stopped(db_session):
    db=db_session;b=batch(db);listing(db,b);db.commit();receipt(db,b)
    db.add(IngestJob(batch_type='for_sale',filename='hougarden_enrich',batch_id=b.id,status='paused',stage='retry'));db.commit()
    with pytest.raises(ValueError,match='queued or running'):validate_release(db,'Auckland')


def test_worker_crash_preserves_saved_fields_and_resumes_same_job(db_session,monkeypatch):
    import requested_stages as worker
    import staged_stages as stages
    db=db_session;b=batch(db);p=listing(db,b)
    j=worker.create_requested_stage(db,stage='enrich',batch_id=b.id,region='Auckland',uploaded_by_id=None)
    def interrupted(jid,bid,region):
        with worker.SessionLocal() as session:
            row=session.get(PropertyForSale,p.id);row.land_area_m2=800
            job=session.get(IngestJob,jid);payload=json.loads(job.result_json);payload['ids']=[p.id];job.result_json=json.dumps(payload);job.status='running';session.commit()
        raise RuntimeError('synthetic worker restart')
    monkeypatch.setattr(stages,'run_enrich_job',interrupted)
    with pytest.raises(RuntimeError,match='synthetic'):worker.run_pending()
    def resumed(jid,bid,region):
        assert jid==j.id and bid==b.id
        with worker.SessionLocal() as session:
            assert session.get(PropertyForSale,p.id).land_area_m2==800
            job=session.get(IngestJob,jid);assert json.loads(job.result_json)['ids']==[p.id];job.status='completed';session.commit()
    monkeypatch.setattr(stages,'run_enrich_job',resumed)
    worker.run_pending();db.refresh(j);db.refresh(p)
    assert j.status=='completed' and p.land_area_m2==800 and json.loads(j.result_json)['worker_attempts']==2


def test_admin_preview_remains_available_but_customer_cannot_request_it(db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.properties import router
    from db import get_db
    from models import User
    from security import create_access_token
    db=db_session;b=batch(db,status='preview');p=listing(db,b)
    admin=User(email='preview@synthetic.test',password_hash='not-a-login',role='admin',status='approved');db.add(admin);db.commit()
    token,_=create_access_token(admin.id)
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get(f'/api/properties/{p.id}?preview=1').status_code==404
        assert client.get(f'/api/properties/{p.id}?preview=1',headers={'Authorization':f'Bearer {token}'}).status_code==200


def test_publish_approves_only_the_selected_sold_delivery(db_session):
    from release import publish_release
    db=db_session;old=batch(db,'sold');old.rows_inserted=1
    selected=batch(db,'sold');selected.rows_inserted=2;db.commit()
    publish_release(db,'Auckland')
    assert selected.status=='published' and old.status=='superseded' and not old.is_active
    assert approved_sold_batch_ids(db,'Auckland')==[selected.id]


def test_legacy_reaper_does_not_fail_worker_owned_requests(db_session):
    from datetime import timedelta
    from job_reaper import reap_stuck_jobs
    db=db_session;b=batch(db)
    j=IngestJob(batch_type='for_sale',filename=f'price (batch {b.id})',batch_id=b.id,status='pending',stage='price',created_at=datetime.now(timezone.utc)-timedelta(days=1),result_json=json.dumps({'requested_stage':{'version':1,'stage':'price','region':'Auckland'}}))
    db.add(j);db.commit();reap_stuck_jobs(db);db.refresh(j);assert j.status=='pending'


def test_custom_admin_hold_survives_repricing_rules(db_session):
    from routers.release import hold_listing
    from release import hold_flagged_rows
    db=db_session;b=batch(db);p=listing(db,b);db.commit()
    hold_listing(p.id,'Do not show until vendor confirms',None,db)
    hold_flagged_rows(db,b.id)
    assert p.is_held and p.hold_reason=='Admin hold: Do not show until vendor confirms'


def test_map_uses_same_canonical_property_type_as_summary(db_session):
    from routers.properties import map_points
    db=db_session;b=batch(db,status='published',active=True)
    for kind in ['House','独立屋','Apartment']:
        listing(db,b,property_type=kind,latitude=-36.8,longitude=174.7)
    db.commit()
    result=map_points(dataset='for_sale',region='Auckland',suburb='Riverhead',min_beds=None,limit=20000,db=db,ptype='House')
    assert result.count==2
