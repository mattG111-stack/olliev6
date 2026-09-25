import json
import httpx
import pytest
from config import settings
from portals.direct import Transport, CollectorUnavailable, collect, merge_records, canonical, status
from portals import page_data

@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr('portals.direct.time.sleep', lambda _: None)


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

@pytest.mark.parametrize('code',[401,403,429])
def test_denials_stop_source_no_rotation_or_retry(code):
    calls=[]
    def handler(r):
        calls.append(r.url.path)
        return httpx.Response(200,text='User-agent: *\nAllow: /') if r.url.path=='/robots.txt' else httpx.Response(code)
    t=Transport('oneroof',client=client_for(handler))
    with pytest.raises(CollectorUnavailable): t.get('https://www.oneroof.co.nz/search')
    with pytest.raises(CollectorUnavailable): t.get('https://www.oneroof.co.nz/search')
    assert calls==['/robots.txt','/search']


def test_robot_disallow_and_redirect_to_other_host_never_fetch_target():
    calls=[]
    def handler(r):
        calls.append(str(r.url))
        return httpx.Response(200,text='User-agent: *\nDisallow: /private') if r.url.path=='/robots.txt' else httpx.Response(302,headers={'Location':'http://127.0.0.1/secrets'})
    t=Transport('trademe',client=client_for(handler))
    with pytest.raises(CollectorUnavailable):t.get('https://www.trademe.co.nz/private')
    assert len(calls)==1
    with pytest.raises(CollectorUnavailable):t.get('https://www.trademe.co.nz/a/property')
    assert len(calls)==2


def test_proxy_exception_redacted():
    def handler(r): raise RuntimeError('http://user:secret@proxy.example')
    t=Transport('homes',client=client_for(handler))
    with pytest.raises(CollectorUnavailable) as got:t.get('https://homes.co.nz/address')
    assert 'secret' not in str(got.value)


def test_settings_status_never_contains_credentials(monkeypatch):
    monkeypatch.setattr(settings,'scraper_proxy_urls','["http://user:secret@proxy.example"]')
    assert 'secret' not in json.dumps(status())


def row(source='oneroof', **kw):
    return dict(source=source,url='https://'+source+'.co.nz/property/1',scraped_at='2026-09-25T12:00:00Z',
                address='1/25 Example Road',suburb='Mount Wellington',district='Auckland City',region='Auckland',
                kind='for_sale',_apex_direct=True,**kw)


def test_merge_fills_blanks_keeps_unit_and_price_conflicts_and_all_raw():
    a=row(price_numeric=900000,raw_source={'unknown_field':'preserved'})
    b=row('realestate',beds=3,price_numeric=950000)
    c={**row('trademe',beds=4),'address':'2/25 Example Rd'}
    d=row('homes',land_area_m2=450,homes_estimate=1000000)
    merged=merge_records([a,b,c,d])
    assert len(merged)==2
    m=merged[0]
    assert m['beds']==3 and m['land_area_m2']==450
    assert m['price_numeric']==900000 and m['conflicts']['price_numeric'][0]['value']==950000
    assert m['homes_estimate']==1000000 and 'estimate' not in m
    assert m['provenance']['beds'][0]['source']=='realestate'
    assert m['source_snapshots'][0]['raw_source']['unknown_field']=='preserved'
    assert 'review' in m['price_flag']


def test_sale_rent_and_dated_sales_do_not_merge():
    a=row(price_numeric=900000)
    b={**row('realestate',rent_weekly=700),'kind':'rent'}
    c={**row('trademe',sale_price=800000),'kind':'sold','sold_date':'2025-01-01'}
    d={**row('homes',sale_price=850000),'kind':'sold','sold_date':'2026-01-01'}
    assert len(merge_records([a,b,c,d]))==4


def test_ambiguous_address_does_not_merge():
    a={**row(),'address':'25-27 Example Road'}
    b={**row('homes'),'address':'25-27 Example Road'}
    assert len(merge_records([a,b]))==2

@pytest.mark.parametrize('source',['oneroof','realestate','trademe','homes'])
def test_public_schema_preserves_only_explicit_prices(source):
    url=f'https://www.{source if source != "realestate" else "realestate"}.co.nz/property/1'
    data={'@type':'RealEstateListing','url':url,'about':{'@type':'House','address':{'streetAddress':'25 Example Road','addressLocality':'Mount Wellington','addressCounty':'Auckland City','addressRegion':'Auckland'},'numberOfBedrooms':3},'offers':{'price':900000,'priceCurrency':'NZD'}}
    text='<script type="application/ld+json">'+json.dumps(data)+'</script>'
    raw=page_data.extract(text,source)[url]
    n=canonical(source,'for_sale',url,raw)
    assert n['price_numeric']==900000 and n['beds']==3
    assert 'sale_price' not in canonical(source,'sold',url,raw)
    assert 'rent_weekly' not in canonical(source,'rent',url,raw)


def test_review_staging_preserves_unmapped_fields(db_session):
    from portals.listings import to_listing, record
    from models import PortalListing, PropertyForSale
    m=merge_records([row(beds=3,raw_source={'new_field':'kept'})])[0]
    staged=to_listing(m['source'],m)
    before=db_session.query(PropertyForSale).count()
    assert record(db_session,[staged])[0]==1
    stored=db_session.query(PortalListing).filter_by(address_key=staged['address_key']).one()
    assert stored.status=='pending'
    assert json.loads(stored.raw_json)['source_snapshots'][0]['raw_source']['new_field']=='kept'
    assert db_session.query(PropertyForSale).count()==before
    assert record(db_session,[staged])[0]==0


def test_schema_drift_is_failure_not_empty_success(monkeypatch):
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'homes':{'for_sale':['https://homes.co.nz/search']}}))
    class Empty:
        def get(self,url): return '<html>layout changed</html>'
    with pytest.raises(CollectorUnavailable,match='adapter needs checking'):
        collect('homes',transport=Empty())
