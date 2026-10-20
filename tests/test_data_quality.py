import csv
import io
import json
from types import SimpleNamespace
import pandas as pd
import pytest
from data_quality import checks, multiple_properties, nz_coordinates, subdivision_checks
from ingest import _common_property_payload, ingest_for_sale
from models import ImportBatch, PortalFinding, PropertyForSale
from preflight_file import check
from tests.test_the_link_check_gets_round_the_book import _sold, _for_sale

@pytest.mark.parametrize('lat,lng',[(-36.8,174.7),(-37.2,174.9)])
def test_valid_coordinates_preserved(lat,lng):
    assert nz_coordinates(lat,lng)
    p=_common_property_payload({'latitude':lat,'longitude':lng})
    assert (p['latitude'],p['longitude'])==(lat,lng)

@pytest.mark.parametrize('lat,lng',[(42.902,-75.157),(27.45,-80.32),(-36.8,None),(float('nan'),174.7)])
def test_invalid_coordinates_do_not_enter_map(lat,lng):
    p=_common_property_payload({'latitude':lat,'longitude':lng})
    assert p['latitude'] is None and p['longitude'] is None

def test_exact_unit_is_not_multi_property_bundle():
    assert not multiple_properties('2/24 Lomandra Street')
    assert not multiple_properties('69A Golf Road')
    assert multiple_properties('Lot 1-14/9 Marriott Road & 9 Cheris Place')
    assert multiple_properties('12A&B Mchardy Place')
    assert multiple_properties('26-58 Ramwall Place')

def test_preflight_exposes_all_gaps_and_original_source():
    row={'address':'12A&B Mchardy Place','suburb':'Glenfield','region':'Auckland',
         'property_type':'House','key_bedrooms':3,'key_bathrooms':1,'latitude':27.45,'longitude':-80.32,
         'sale_method':'auction','price_numeric':1200000,'property_valuation_numeric':1200000,
         'url':'https://www.hougarden.com/nz/property/auckland/glenfield/test/abc','last_updated':'2026-10-13'}
    data,counts=check(pd.DataFrame([row]))
    result=list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))[0]
    assert counts['_loaded']==0
    assert all(s in result['All data issues'] for s in ['Missing CV','Missing floor area','Missing land area','Invalid NZ coordinates','Multiple properties'])
    assert result['Sale method']=='auction' and result['Portal estimate']=='1200000'
    assert result['Asking']=='' and result['Raw feed price']=='1200000.0'
    assert result['Source date']=='2026-10-13'
    assert 'Service capacity: not verified' in result['Subdivision evidence']

def test_import_preserves_offered_price_before_auction_pricing(db_session):
    frame=_for_sale(['fresh'])
    frame['sale_method']='auction';frame['property_valuation_numeric']=1100000
    frame['last_updated']='2026-10-13';frame['latitude']=42.902;frame['longitude']=-75.157
    result=ingest_for_sale(db_session,frame,_sold(),'fresh.csv',publish=False,fill_missing=False)
    p=db_session.query(PropertyForSale).filter_by(import_batch_id=result.batch_id).one()
    assert p.asking_price is None and p.listing_type=='auction'
    assert p.latitude is None and p.longitude is None
    evidence=db_session.query(PortalFinding).filter_by(property_id=p.id,field='import_record').one()
    raw=json.loads(evidence.extra_json)
    assert raw['original']['price_numeric']==1100000
    assert raw['original']['latitude']==42.902
    assert raw['source_date']=='2026-10-13' and raw['filename']=='fresh.csv'
    assert raw['captured_at']

def test_preflight_does_not_reject_hidden_auction_price_as_bad_asking():
    frame=_for_sale(['auction']);frame['sale_method']='auction';frame['price_numeric']=100000
    data,counts=check(frame)
    row=list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))[0]
    assert counts['_loaded']==1 and row['Asking']=='' and row['Raw feed price']=='100000.0'

def test_carry_preserves_original_observation_date(db_session):
    first=ingest_for_sale(db_session,_for_sale(['old']),_sold(),'first.csv',publish=True,fill_missing=False)
    old=db_session.query(PropertyForSale).filter_by(import_batch_id=first.batch_id).one()
    evidence=db_session.query(PortalFinding).filter_by(property_id=old.id,field='import_record').one()
    second=ingest_for_sale(db_session,_for_sale(['new']),_sold(),'second.csv',publish=False,fill_missing=False)
    carried=db_session.query(PropertyForSale).filter_by(import_batch_id=second.batch_id,slug_id='old').one()
    kept=db_session.query(PortalFinding).filter_by(property_id=carried.id,field='import_record').one()
    assert kept.extra_json==evidence.extra_json
    assert json.loads(kept.extra_json)['filename']=='first.csv'

def test_quality_routes_require_login():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.release import router
    app=FastAPI();app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/api/admin/release/data-quality').status_code==401
        assert client.post('/api/admin/release/data-quality/repair').status_code==401

def test_live_report_readonly_and_repair_retains_bad_coordinates(db_session):
    from routers.release import data_quality_report,repair_data_quality
    b=ImportBatch(batch_type='for_sale',filename='live.csv',region='Auckland',status='published',is_active=True)
    db_session.add(b);db_session.flush()
    p=PropertyForSale(import_batch_id=b.id,address='12A&B Mchardy Place',suburb='Glenfield',latitude=27.45,longitude=-80.32,is_held=False)
    good=PropertyForSale(import_batch_id=b.id,address='2/24 Lomandra Street',suburb='Westgate',latitude=-36.8,longitude=174.7,is_held=False)
    db_session.add_all([p,good]);db_session.commit()
    report=data_quality_report(region='Auckland',limit=50,_=None,db=db_session)
    assert report['batch_id']==b.id and report['status']=='published'
    assert p.latitude==27.45 and not p.is_held
    result=repair_data_quality(region='Auckland',admin=SimpleNamespace(id=None),db=db_session)
    assert result['invalid_pins_excluded']==1 and result['ambiguous_listings_held']==1
    # Repairs are private until the next approved release.
    assert p.latitude == 27.45 and not p.is_held
    draft=db_session.query(ImportBatch).filter_by(status='staged').one()
    repaired=db_session.query(PropertyForSale).filter_by(import_batch_id=draft.id,address=p.address).one()
    assert repaired.latitude is None and repaired.is_held and not good.is_held
    raw=json.loads(db_session.query(PortalFinding).filter_by(property_id=repaired.id,field='coordinates').one().extra_json)
    assert raw['previous_latitude']==27.45 and raw['previous_longitude']==-80.32
    assert repair_data_quality(region='Auckland',admin=SimpleNamespace(id=None),db=db_session)['invalid_pins_excluded']==0

def test_failed_single_enrichment_never_stamped_as_checked(db_session,monkeypatch):
    import staged_stages as stages
    b=ImportBatch(batch_type='for_sale',filename='test.csv',region='Auckland')
    db_session.add(b);db_session.flush()
    p=PropertyForSale(import_batch_id=b.id,address='8 Example Road',suburb='Oneroa')
    db_session.add(p);db_session.commit();seen=[]
    monkeypatch.setattr(stages,'pv_lookup_status',lambda q:(seen.append(q) or None,'error'))
    stages.enrich_one(db_session,p.id)
    assert p.pv_checked_at is None and seen==['8 Example Road, Oneroa']

def test_single_enrichment_records_each_filled_fact(db_session,monkeypatch):
    import staged_stages as stages
    b=ImportBatch(batch_type='for_sale',filename='test.csv',region='Auckland')
    db_session.add(b);db_session.flush()
    p=PropertyForSale(import_batch_id=b.id,address='24 Lomandra Street',suburb='Westgate',floor_area_m2=155)
    db_session.add(p);db_session.commit()
    monkeypatch.setattr(stages,'pv_lookup_status',lambda q:({'land_area_m2':405,'floor_area_m2':165,'year_built':2017,'car_spaces':2,'url':'https://propertyvalue.co.nz/record'},'ok'))
    stages.enrich_one(db_session,p.id)
    assert p.floor_area_m2==155 and p.land_area_m2==405
    assert str(p.building_age)=='2017' and p.cars==2
    finding=db_session.query(PortalFinding).filter_by(property_id=p.id,field='land_area_m2').one()
    assert finding.source=='corelogic' and finding.value_num==405 and finding.decided_at
    assert json.loads(finding.extra_json)['url']=='https://propertyvalue.co.nz/record'

def test_cost_estimate_never_claims_services_verified():
    p=SimpleNamespace(services_cost=130000,zoning='Mixed Housing Suburban',type_of_title='Freehold',land_area_flag=None)
    assert 'Service capacity: not verified' in subdivision_checks(p)

def test_retained_corelogic_wrong_unit_is_an_identity_failure():
    from data_quality import identity_mismatch,source_disagreements
    p=PropertyForSale(address='2/24 Lomandra Street',suburb='Westgate',pv_data=json.dumps({
        'canonical_address':'1/24 Lomandra Street, Westgate','suburb':'Westgate','floor_area_m2':140}))
    assert identity_mismatch(p)
    assert 'CoreLogic record belongs to a different exact address/unit' in checks(p)
    assert not source_disagreements(p)  # never compare a different home's facts

def test_source_disagreements_retained_without_overwriting():
    from data_quality import source_disagreements
    p=PropertyForSale(address='24 Lomandra Street',suburb='Westgate',land_area_m2=5665,
        pv_data=json.dumps({'canonical_address':'24 Lomandra Street, Westgate','suburb':'Westgate','land_area_m2':416}))
    assert source_disagreements(p)[0]['field']=='land_area_m2'
    assert p.land_area_m2==5665
