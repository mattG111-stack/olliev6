import json
import pytest
from config import settings
from portals.direct import collect, CollectorUnavailable, Transport
from portals.rendered import property_links, proxy_options


def test_only_public_auckland_property_links_are_discovered():
    html=''.join('<a href="'+u+'">Property</a>' for u in [
        '/address/auckland/example/1/abc', '/address/auckland/example/1/abc?track=yes',
        '/address/wellington/example/1/abc', 'https://evil.example/address/auckland/1',
        '/profile/agent', '/map'])
    assert property_links(html,'https://homes.co.nz/map')==['https://homes.co.nz/address/auckland/example/1/abc']


def test_proxy_credentials_are_separate_from_server_url():
    p=proxy_options('http://synthetic:pass%40word@gateway.example:10001')
    assert p=={'server':'http://gateway.example:10001','username':'synthetic','password':'pass@word'}


def test_rendered_discovery_feeds_existing_detail_parser(monkeypatch):
    url='https://homes.co.nz/map?filter=type:sold'
    detail='https://homes.co.nz/address/auckland/example/1/abc'
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'homes':{'sold':[url]}}))
    from portals import page_data
    monkeypatch.setattr(page_data,'extract',lambda html,source: {detail:{'_apex_kind':'sold'}} if html=='DETAIL' else {})
    monkeypatch.setattr(page_data,'normalise',lambda *args:{'address':'1 Example Road','suburb':'Example','region':'Auckland','sale_price':900000,'sold_date':'2026-01-01','scraped_at':'2026-09-26'})
    calls=[]
    class Pages:
        def get(self,target):
            calls.append(target)
            return '<a href="'+detail+'">Property</a>' if target==url else 'DETAIL'
    rows=collect('homes',kind='sold',transport=Pages())
    assert calls==[url,detail]
    assert len(rows)==1 and rows[0]['sale_price']==900000


def test_denied_preflight_never_calls_renderer(monkeypatch):
    import httpx
    calls=[]
    monkeypatch.setattr(settings,'scraper_render_homes',True)
    monkeypatch.setattr('portals.rendered.render_homes',lambda *a,**k:calls.append(1))
    monkeypatch.setattr('portals.direct.time.sleep',lambda _:None)
    client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='User-agent: *\nDisallow: /map') if req.url.path=='/robots.txt' else httpx.Response(200,text='page')))
    with pytest.raises(CollectorUnavailable,match='robots'):
        Transport('homes',client=client).get('https://homes.co.nz/map')
    assert not calls


def test_actual_browser_waits_for_javascript_links_and_loading_state():
    from playwright.sync_api import sync_playwright
    from portals.rendered import rendered_html
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('''<body><div role="progressbar">Loading</div><script>
            setTimeout(()=>{document.querySelector('[role="progressbar"]').remove();
            document.body.insertAdjacentHTML('beforeend','<a href="/address/auckland/example/1/abc">Sold property</a>');},100);
            </script></body>''')
            html=rendered_html(page,'https://homes.co.nz/map',3000,[])
            assert property_links(html,'https://homes.co.nz/map')==['https://homes.co.nz/address/auckland/example/1/abc']
            page.set_content('<body>Verify you are human</body>')
            with pytest.raises(CollectorUnavailable,match='restriction'):
                rendered_html(page,'https://homes.co.nz/map',3000,[])
        finally:browser.close()


def test_discovery_without_budget_for_details_is_not_empty_success(monkeypatch):
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'homes':{'sold':['https://homes.co.nz/map']}}))
    monkeypatch.setattr(settings,'scraper_max_pages',1)
    class Pages:
        def get(self,url):return '<a href="/address/auckland/example/1/abc">Property</a>'
    with pytest.raises(CollectorUnavailable,match='budget exhausted'):
        collect('homes',kind='sold',transport=Pages())


def test_actual_browser_loads_more_homes_results_and_reports_partial_coverage():
    from playwright.sync_api import sync_playwright
    from portals.rendered import load_homes_results,discovery_info
    fixture='''<body>3 properties<div class="drawerContentContainer" style="height:100px;overflow:auto">
      <div style="height:400px"><a href="/address/auckland/example/1/abc">One</a></div>
      </div><script>let n=1;const box=document.querySelector('.drawerContentContainer');
      box.addEventListener('scroll',()=>{if(n<3){n++;box.insertAdjacentHTML('beforeend',
      '<div style="height:400px"><a href="/address/auckland/example/'+n+'/abc">Next</a></div>');}});
      </script></body>'''
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content(fixture)
            html=load_homes_results(page,'https://homes.co.nz/map',3000,[],max_batches=2)
            assert discovery_info(html)=={'loaded':2,'total':3,'complete':False}
            page.close();page=browser.new_page();page.set_content(fixture)
            html=load_homes_results(page,'https://homes.co.nz/map',3000,[],max_batches=3)
            assert discovery_info(html)=={'loaded':3,'total':3,'complete':True}
        finally:browser.close()


def test_coverage_metadata_preserves_unknown_totals():
    from portals.rendered import discovery_info
    assert discovery_info('<meta name="apex-homes-coverage" data-loaded="20" data-total="unknown" data-complete="false">')=={'loaded':20,'total':None,'complete':False}
