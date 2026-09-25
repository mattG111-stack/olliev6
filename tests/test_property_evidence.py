import json
from models import ImportBatch, BatchType, PortalListing, PropertyForSale
from portals.direct import merge_records
from portals.listings import to_listing, record, approve


def rows():
    common={'_apex_direct':True,'kind':'for_sale','address':'1 Test Road','suburb':'Example',
            'district':'Auckland City','region':'Auckland','price_numeric':900000}
    return [{**common,'source':'oneroof','url':'https://www.oneroof.co.nz/property/example',
             'oneroof_estimate':1100000,'beds':3,'sale_history':[
                 {'saleDate':'2017-01-01','salePrice':650000}]},
            {**common,'source':'homes','url':'https://homes.co.nz/address/auckland/example/1/abc',
             'homes_estimate':1050000,'land_slope_contour':'Level','building_age_decade':'1990',
             'floor_area_m2':140,'land_area_m2':600,'sale_history':[
                 {'saleDate':'2017-01-01','salePrice':650000},
                 {'saleDate':'2022-03-01','salePrice':850000}]}]


def test_merge_fills_missing_facts_and_unions_sales_without_comparing_estimates():
    item=merge_records(rows())[0]
    assert not item['conflicts']
    assert item['beds']==3 and item['floor_area_m2']==140 and item['land_area_m2']==600
    assert len(item['sale_history'])==2
    assert len(item['sale_history'][1]['sources'])==2
    assert item['oneroof_estimate']==1100000 and item['homes_estimate']==1050000


def test_approval_keeps_both_source_estimates_history_and_contour(db_session):
    batch=ImportBatch(batch_type=BatchType.FOR_SALE.value,region='Auckland',filename='synthetic',is_active=True,status='published')
    db_session.add(batch);db_session.commit()
    item=merge_records(rows())[0]
    record(db_session,[to_listing(item['source'],item)])
    row=db_session.query(PortalListing).one()
    ok,why=approve(db_session,row.id,reprice=lambda db,pid:None)
    assert ok,why
    prop=db_session.get(PropertyForSale,row.property_id)
    assert prop.oneroof_valuation==1100000 and prop.homes_valuation==1050000
    assert prop.fair_value is None  # Source estimates never become our price.
    assert prop.homes_url==rows()[1]['url']
    assert prop.land_slope_contour=='Level' and prop.building_age=='1990s'
    assert len(json.loads(prop.sale_history_json))==2
    assert prop.valuation_last_sold_value==850000
    assert prop.valuation_last_sold_date=='2022-03-01'


def test_same_date_price_conflict_retained_but_different_dates_not_combined():
    source=rows();source[1]['sale_history'][0]['salePrice']=700000
    item=merge_records(source)[0]
    assert item['conflicts']['sale_history']==[{'date':'2017-01-01','prices':[650000,700000]}]
    assert len(item['sale_history'])==3 and item['price_flag']


def test_disputed_history_is_not_presented_as_an_agreed_transaction():
    from portals.evidence import apply_to_property
    source=rows();source[1]['sale_history'][0]['salePrice']=700000
    item=merge_records(source)[0]
    prop=PropertyForSale()
    apply_to_property(prop,json.dumps(item))
    assert json.loads(prop.sale_history_json)==[{'saleDate':'2022-03-01','salePrice':850000}]
    assert len(item['sale_history'])==3  # Full conflicting evidence remains.


def test_disclosed_history_price_fills_same_dated_undisclosed_sale():
    source=rows();source[0]['sale_history'][0]['salePrice']=None
    item=merge_records(source)[0]
    assert len(item['sale_history'])==2
    assert item['sale_history'][1]['salePrice']==650000
    assert len(item['sale_history'][1]['sources'])==2
    assert not item['conflicts']


def test_missing_district_can_join_one_exact_property_but_not_ambiguous_or_other_unit():
    source=rows();source[1].pop('district')
    merged=merge_records(source)
    assert len(merged)==1 and merged[0]['district']=='Auckland City'
    assert 'district' not in source[1]  # Missing source fact stays missing.
    ambiguous={**source[0],'district':'Other district','url':'https://www.oneroof.co.nz/property/other'}
    assert len(merge_records([*source,ambiguous]))==3
    source[1]['address']='2/1 Test Road'
    assert len(merge_records(source))==2
