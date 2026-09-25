import json
import httpx
import pytest
from portals.page_data import extract
from portals.direct import canonical, merge_records, Transport, CollectorUnavailable


def page(records):
    return '<script type="application/json">'+json.dumps(records)+'</script>'


def trademe():
    return dict(listingId=123,canonicalPath='/property/residential/sale/auckland/auckland-city/example/listing/123',
        region='Auckland',suburb='Auckland City',startPrice=1,priceDisplay='Auction on 13 Oct',
        startDate='__date__:2026-09-15T03:42:00Z',body='Example description',
        attributes=[{'name':k,'value':v} for k,v in {
            'location':'1/25 Example Road, Example, Auckland City, Auckland',
            'suburb':'Example','district':'Auckland City','region':'Auckland','bedrooms':'3 bedrooms',
            'bathrooms':'1 bathroom','floor_area':'109m²','land_area':'502m²','garage_parking':'1',
            'off-street_parking':'3','rateable_value_(rv)':'$970,000'}.items()],
        propertySaleInformation={'propertyDateOfSale':'__date__:2026-10-13T00:00:00Z'},
        localSales={'recentSales':[{'salePrice':2400000}]})


def homes():
    return dict(property_id='example',listing_id='listing',state=0,
        url='/auckland/example/1-25-example-road/abcd',display_price='Auction on 13 Oct',
        property_details={'display_address':'1/25 Example Road, Example, Auckland City, Auckland',
            'suburb':'EXAMPLE','num_bedrooms':3,'num_bathrooms':1,'num_car_spaces':4,'floor_area':109,
            'land_area':502,'decade_built':'1920','capital_value':920000,
            'display_estimated_value_short':'745K','current_revision_date':'2024-05-01'})


def normalized(source,record,kind='for_sale'):
    url,raw=next(iter(extract(page([record]),source).items()))
    return canonical(source,kind,url,raw)


def test_detail_display_location_overrides_trademe_top_level_district_in_suburb():
    n=normalized('trademe',trademe())
    assert n['address']=='1/25 Example Road' and n['suburb']=='Example'
    assert n['beds']==3 and n['baths']==1 and n['carspaces']==4
    assert n['floor_area_m2']==109 and n['land_area_m2']==502
    assert n['listed_date']=='2026-09-15'
    assert 'price_numeric' not in n and 'sale_price' not in n and 'sold_date' not in n
    assert 'beds' in n['provenance'] and 'key_bedrooms' not in n['provenance']


def test_homes_decade_not_claimed_as_exact_year_and_conflicts_preserved():
    a=normalized('trademe',trademe());b=normalized('homes',homes())
    assert b['building_age_decade']=='1920' and 'building_age' not in b
    assert b['homes_estimate']==745000 and 'price_numeric' not in b
    merged=merge_records([a,b])
    assert len(merged)==1
    assert merged[0]['homes_estimate']==745000
    assert merged[0]['conflicts']['cv_numeric'][0]['value']==920000
    assert len(merged[0]['source_snapshots'])==2


def test_recommendations_without_listing_identity_and_unknown_homes_states_ignored():
    assert not extract(page([{'listingId':123,'price':123000}]),'trademe')
    assert not extract(page([{**homes(),'state':99}]),'homes')
    with pytest.raises(ValueError):normalized('trademe',trademe(),'sold')


@pytest.mark.parametrize('display,expected',[('Price by negotiation',None),('$859,000',859000),('Asking price $859,000',859000)])
def test_only_explicit_advertised_price_used(display,expected):
    n=normalized('homes',{**homes(),'display_price':display,'price':1})
    assert n.get('price_numeric')==expected


def test_waf_202_is_access_challenge_not_empty_page_and_stops_retries(monkeypatch):
    monkeypatch.setattr('portals.direct.time.sleep',lambda _:None)
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path=='/robots.txt':return httpx.Response(200,text='User-agent: *\nAllow: /')
        return httpx.Response(202,headers={'x-amzn-waf-action':'challenge'})
    t=Transport('realestate',client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(CollectorUnavailable,match='access challenge'):t.get('https://www.realestate.co.nz/residential/sale/auckland')
    with pytest.raises(CollectorUnavailable,match='paused'):t.get('https://www.realestate.co.nz/residential/sale/auckland')
    assert len(calls)==2


@pytest.mark.parametrize('display,expected',[('$TBC',None),('$600,100',600100),('To be provided',None)])
def test_homes_sold_prices_never_use_estimates_or_undisclosed_internal_price(display,expected):
    record={**homes(),'state':1,'display_price':display,'price':999999,'date':'2026-08-18T00:00:00Z'}
    n=normalized('homes',record,'sold')
    assert n.get('sale_price')==expected and n['sold_date']=='2026-08-18'
    assert 'listed_date' not in n and 'price_numeric' not in n
    from portals.listings import to_listing
    assert (to_listing('homes',n,kind='sold') is not None)==(expected is not None)


def test_trademe_insights_sold_schema_and_region_without_invented_district():
    record={'propertyId':'synthetic-id','state':1,'date':'2026-08-18T00:00:00Z','price':600100,
            'displayPrice':'$600,100','url':'/property/insights/profile/auckland/example/1-25-example-road/synthetic-id',
            'propertyDetails':{'displayAddress':'1/25 Example Road, Example, Auckland',
                'city':'AUCKLAND','suburb':'EXAMPLE','numBedrooms':2,'displayEstimatedValueShort':'750K'}}
    n=normalized('trademe',record,'sold')
    assert n['sale_price']==600100 and n['sold_date']=='2026-08-18'
    assert n['region']=='Auckland' and not n.get('district')
    assert n['beds']==2 and n['homes_estimate']==750000
