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


@pytest.mark.parametrize("windows,total,expected_complete", [
    ([['a'], ['b'], ['c']], 3, True),
    ([['a','b'], ['b','c'], ['a','b']], 4, False),
])
def test_virtualized_windows_keep_every_observed_link(monkeypatch, windows, total, expected_complete):
    from portals import rendered
    base = 'https://homes.co.nz/map'
    class Page:
        index = 0
        def locator(self, selector):
            self.selector = selector
            return self
        def inner_text(self): return f'{total} properties'
        def count(self): return 1
        def evaluate(self, script):
            if 'scrollTop=el.scrollHeight' in script:
                self.index = min(self.index + 1, len(windows)-1)
                return None
            return {'top':self.index * 75,'height':1000,'viewport':100}
        def wait_for_function(self, script, *, arg, timeout):
            assert isinstance(arg["known"], list)  # Compare identities, not current DOM length.
            if not any(url(x) not in arg["known"] for x in windows[self.index]):
                raise TimeoutError('no unseen links')
    def url(x): return f'https://homes.co.nz/address/auckland/example/{x}/abc'
    page = Page()
    monkeypatch.setattr(rendered, 'rendered_html', lambda *args:
        ''.join(f'<a href="{url(x)}">Listing</a>' for x in windows[page.index]))
    html = rendered.load_homes_results(page, base, 100, [], max_batches=3)
    assert set(rendered.property_links(html, base)) == {url(x) for w in windows for x in w}
    assert rendered.discovery_info(html) == {'loaded':3,'total':total,'complete':expected_complete}


def test_actual_browser_preserves_replaced_result_cards():
    from playwright.sync_api import sync_playwright
    from portals.rendered import load_homes_results, discovery_info
    fixture = '<body>3 properties<div class="drawerContentContainer" style="height:100px;overflow:auto">\n    <div style="height:400px"><a href="/address/auckland/example/1/abc">One</a></div></div>\n    <script>let n=1;const box=document.querySelector(\'.drawerContentContainer\');\n    box.addEventListener(\'scroll\',()=>{if(box.scrollTop>0 && n<3){n++;\n    box.innerHTML=\'<div style="height:400px"><a href="/address/auckland/example/\'+n+\'/abc">Next</a></div>\';box.scrollTop=0;}});</script></body>'
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(fixture)
            html = load_homes_results(page,'https://homes.co.nz/map',3000,[],max_batches=3)
            assert len(property_links(html,'https://homes.co.nz/map')) == 3
            assert discovery_info(html) == {'loaded':3,'total':3,'complete':True}
        finally:
            browser.close()


def test_discovery_advances_through_overlapping_virtual_windows(monkeypatch):
    from portals import rendered
    base='https://homes.co.nz/map'
    def link(n):return f'https://homes.co.nz/address/auckland/example/{n}/abc'
    class Panel:
        top=0
        def count(self):return 1
        def evaluate(self, script):
            if 'scrollTop=el.scrollHeight' in script:
                self.top=min(300,self.top+75)
                return None
            return {'top':self.top,'height':400,'viewport':100}
    class Page:
        def __init__(self):self.panel=Panel()
        def locator(self, selector):return self.panel if selector=='.drawerContentContainer' else self
        def inner_text(self):return '5 properties'
        def wait_for_function(self, script, *, arg, timeout):
            # Overlapping windows can have no new links on a given scroll.
            if self.panel.top in (75,225):raise TimeoutError('overlap')
    page=Page()
    windows={0:[1],75:[1],150:[2],225:[2],300:[3]}
    monkeypatch.setattr(rendered,'rendered_html',lambda *args:
        ''.join(f'<a href="{link(n)}">Sold</a>' for n in windows[page.panel.top]))
    html=rendered.load_homes_results(page,base,100,[],max_batches=6)
    assert rendered.property_links(html,base)==[link(3),link(1),link(2)]
    assert rendered.discovery_info(html)['complete'] is False


def test_partial_homes_coverage_advances_next_bounded_pass(monkeypatch):
    seed='https://homes.co.nz/map/auckland?filter=type:sold'
    detail='https://homes.co.nz/address/auckland/example/1/abc'
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'homes':{'sold':[seed]}}))
    from portals import page_data
    monkeypatch.setattr(page_data,'extract',lambda html,source:
        {detail:{'_apex_kind':'sold'}} if html=='DETAIL' else {})
    monkeypatch.setattr(page_data,'normalise',lambda *args:{'address':'1 Example Road',
        'region':'Auckland','sale_price':900000,'sold_date':'2026-01-01','scraped_at':'2026-09-26'})
    class Pages:
        def get(self,url):
            return ('<a href="'+detail+'">Sold</a><meta name="apex-homes-coverage" '
                    'data-loaded="38" data-total="4932" data-complete="false">') if url==seed else 'DETAIL'
    state={}
    collect('homes',kind='sold',transport=Pages(),checkpoint=state)
    assert state['homes_discovery_batches']==100
    assert state['discovery_coverage']=={'loaded':38,'total':4932,'complete':False}


def test_actual_browser_rearms_latched_infinite_scroll_between_batches():
    """A source may require leaving its bottom zone before another load."""
    from playwright.sync_api import sync_playwright
    from portals.rendered import load_homes_results, discovery_info
    fixture = '''<body>4 properties
      <div class="drawerContentContainer" style="height:100px;overflow:auto">
        <div style="height:400px"><a href="/address/auckland/example/1/abc">One</a></div>
      </div><script>
      let n=1, armed=true;
      const box=document.querySelector('.drawerContentContainer');
      box.addEventListener('scroll',()=>{
        if(box.scrollTop===0){armed=true;return;}
        if(armed && box.scrollTop+box.clientHeight>=box.scrollHeight-1 && n<4){
          armed=false;n++;
          box.insertAdjacentHTML('beforeend','<div style="height:400px"><a href="/address/auckland/example/'+n+'/abc">Next</a></div>');
        }
      });</script></body>'''
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        try:
            page=browser.new_page();page.set_content(fixture)
            html=load_homes_results(page,'https://homes.co.nz/map',3000,[],max_batches=2)
            assert discovery_info(html)=={'loaded':2,'total':4,'complete':False}
            assert len(property_links(html,'https://homes.co.nz/map'))==2
            page.close();page=browser.new_page();page.set_content(fixture)
            html=load_homes_results(page,'https://homes.co.nz/map',3000,[],max_batches=30)
            assert discovery_info(html)=={'loaded':4,'total':4,'complete':True}
            assert len(property_links(html,'https://homes.co.nz/map'))==4
        finally:browser.close()


def test_expanding_homes_window_skips_queued_prefix_and_refreshes_after_completion(monkeypatch):
    from portals.direct import collect, page_data
    seed='https://homes.co.nz/map/auckland?filter=type:sold'
    urls=[f'https://homes.co.nz/address/auckland/example/{i}/abc' for i in range(4)]
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'homes':{'sold':[seed]}}))
    monkeypatch.setattr(settings,'scraper_max_pages',2)
    monkeypatch.setattr(page_data,'extract',lambda text,source:
        {text:{'_apex_kind':'sold'}} if text in urls else {})
    monkeypatch.setattr(page_data,'normalise',lambda source,kind,url,raw:{
        'address':f'{urls.index(url)+1} Example Road','suburb':'Example','region':'Auckland',
        'sale_price':900000,'sold_date':'2026-01-01','scraped_at':'2026-09-26'})
    class Pages:
        width=2
        calls=[]
        def get(self,url):
            self.calls.append(url)
            if url!=seed:return url
            return ''.join(f'<a href="{u}">Sold</a>' for u in urls[:self.width])+(
                f'<meta name="apex-homes-coverage" data-loaded="{self.width}" '
                f'data-total="4" data-complete="{str(self.width==4).lower()}">')
    pages=Pages();state={}
    collect('homes',kind='sold',transport=pages,checkpoint=state)
    assert pages.calls==[seed,urls[0]]
    assert state['pending_urls']==[urls[1]]
    # Mimic persistent JSON storage and restart, then finish unread details.
    state=json.loads(json.dumps(state));pages.calls=[]
    collect('homes',kind='sold',transport=pages,checkpoint=state)
    assert pages.calls==[urls[1]]
    pages.calls=[];pages.width=4
    collect('homes',kind='sold',transport=pages,checkpoint=state)
    assert pages.calls==[seed,urls[2]]
    assert state['pending_urls']==[urls[3]]
    pages.calls=[]
    collect('homes',kind='sold',transport=pages,checkpoint=state)
    assert pages.calls==[urls[3]]
    assert state['homes_discovered_urls']=={}
    # A new pass refreshes old records; the dedupe is not permanent suppression.
    pages.calls=[]
    collect('homes',kind='sold',transport=pages,checkpoint=state)
    assert pages.calls==[seed,urls[0]]


def test_homes_prefix_tracking_does_not_suppress_late_sold_price_recheck(monkeypatch):
    seed='https://homes.co.nz/map/auckland?filter=type:sold'
    detail='https://homes.co.nz/address/auckland/example/1/abc'
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'homes':{'sold':[seed]}}))
    monkeypatch.setattr(settings,'scraper_max_pages',3)
    from portals.direct import page_data
    monkeypatch.setattr(page_data,'extract',lambda text,source:
        {detail:{'_apex_kind':'sold'}} if text=='DETAIL' else {})
    monkeypatch.setattr(page_data,'normalise',lambda *args:{'address':'1 Example Road',
        'suburb':'Example','region':'Auckland','sale_price':900000,
        'sold_date':'2026-01-01','scraped_at':'2026-09-26'})
    class Pages:
        calls=[]
        def get(self,url):
            self.calls.append(url)
            if url==detail:return 'DETAIL'
            return f'<a href="{detail}">Sold</a><meta name="apex-homes-coverage" data-loaded="1" data-total="4" data-complete="false">'
    pages=Pages();state={'homes_discovered_urls':{seed:[detail]},'sold_recheck_urls':[detail]}
    rows=collect('homes',kind='sold',transport=pages,checkpoint=state)
    assert pages.calls==[detail,seed]
    assert rows[0]['sale_price']==900000
    assert state['sold_recheck_urls']==[]
    assert state['discovery_coverage']['complete'] is False


def test_actual_browser_discovery_keeps_urls_without_copying_oversized_card_markup():
    from playwright.sync_api import sync_playwright
    from portals.rendered import rendered_html
    from portals.direct import MAX_BYTES
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('<body><a href="/address/auckland/example/1/abc">Sold</a><div>'+('x'*(MAX_BYTES+1))+'</div></body>')
            with pytest.raises(CollectorUnavailable,match='size limit'):
                rendered_html(page,'https://homes.co.nz/map',3000,[])
            compact=rendered_html(page,'https://homes.co.nz/map',3000,[],True)
            assert property_links(compact,'https://homes.co.nz/map')==['https://homes.co.nz/address/auckland/example/1/abc']
            assert len(compact.encode())<1000
            page.set_content('<body>Access denied<a href="/address/auckland/example/1/abc">Sold</a></body>')
            with pytest.raises(CollectorUnavailable,match='restriction'):
                rendered_html(page,'https://homes.co.nz/map',3000,[],True)
        finally:browser.close()


def test_coverage_uses_latest_visible_total_not_obsolete_initial_count(monkeypatch):
    from portals import rendered
    class Page:
        reads=0
        def locator(self,selector):return self
        def inner_text(self):
            self.reads+=1
            return '1 properties' if self.reads==1 else '2 properties'
    monkeypatch.setattr(rendered,'rendered_html',lambda *args:'<a href="/address/auckland/example/1/abc">Sold</a>')
    html=rendered.load_homes_results(Page(),'https://homes.co.nz/map',100,[],max_batches=1)
    assert rendered.discovery_info(html)=={'loaded':1,'total':2,'complete':False}


def test_trademe_discovery_only_follows_auckland_sale_listings():
    from portals.rendered import trademe_property_links
    root='https://www.trademe.co.nz/a/property/residential/sale/auckland'
    good=root+'/auckland-city/example/listing/123'
    html=''.join(f'<a href="{u}">Property</a>' for u in [good,good+'?tracking=x',
        good.replace('/auckland/','/wellington/'),good.replace('/sale/','/rent/'),
        good.replace('www.trademe.co.nz','evil.example'),root+'?page=2'])
    assert trademe_property_links(html,root)==[good]


def test_trademe_rendered_links_resume_detail_and_next_page(monkeypatch):
    from portals import page_data
    root='https://www.trademe.co.nz/a/property/residential/sale/auckland'
    detail=root+'/auckland-city/example/listing/123';second=root+'?page=2'
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'trademe':{'for_sale':[root]}}))
    monkeypatch.setattr(settings,'scraper_max_pages',2)
    monkeypatch.setattr(page_data,'extract',lambda html,source:{detail:{}} if html=='DETAIL' else {})
    monkeypatch.setattr(page_data,'normalise',lambda *args:{'address':'1 Example Road','suburb':'Example','region':'Auckland','scraped_at':'2026-09-26'})
    calls=[]
    class Pages:
        def get(self,url):
            calls.append(url)
            return f'<a href="{detail}">Property</a><a aria-label="Next page" href="{second}">Next</a>' if url==root else 'DETAIL'
    state={};rows=collect('trademe',cap=1,transport=Pages(),checkpoint=state)
    assert calls==[root,detail] and rows[0]['url']==detail
    assert state['pending_urls']==[second]


def test_trademe_renderer_has_proxy_and_obeys_robots_preflight(monkeypatch):
    import httpx
    calls=[]
    monkeypatch.setattr(settings,'scraper_render_trademe',True)
    monkeypatch.setattr('portals.direct.time.sleep',lambda _:None)
    monkeypatch.setattr('portals.rendered.render_trademe_search',lambda url,proxy:(calls.append(proxy) or '<a>Rendered</a>'))
    root='https://www.trademe.co.nz/a/property/residential/sale/auckland'
    def response(request):return httpx.Response(200,text='User-agent: *\nDisallow:' if request.url.path=='/robots.txt' else 'Enable JavaScript')
    t=Transport('trademe',client=httpx.Client(transport=httpx.MockTransport(response)))
    t.proxy_url='http://synthetic:password@proxy.example:10001'
    assert t.get(root)=='<a>Rendered</a>' and calls==[t.proxy_url]
    calls.clear()
    denied=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='User-agent: *\nDisallow: /a/property')))
    with pytest.raises(CollectorUnavailable,match='robots'):Transport('trademe',client=denied).get(root)
    assert not calls


def test_actual_browser_trademe_discovery_reads_javascript_links_and_stops_on_restriction():
    from playwright.sync_api import sync_playwright
    from portals.rendered import trademe_search_html,trademe_property_links
    from portals.direct import next_pages
    root='https://www.trademe.co.nz/a/property/residential/sale/auckland'
    detail=root+'/auckland-city/example/listing/123'
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content(f'''<body><script>setTimeout(()=>document.body.insertAdjacentHTML('beforeend',
              '<a href="{detail}">Property</a><a href="{root}?page=2" aria-label="Next page, page 2">Next</a>'),100)</script></body>''')
            html=trademe_search_html(page,root,3000,[])
            assert trademe_property_links(html,root)==[detail]
            assert next_pages(html,root,'trademe')==[root+'?page=2']
            with pytest.raises(CollectorUnavailable,match='restriction'):trademe_search_html(page,root,3000,['access'])
            page.set_content(f'<body>Verify you are human<a href="{detail}">Property</a></body>')
            with pytest.raises(CollectorUnavailable,match='restriction'):trademe_search_html(page,root,3000,[])
            page.set_content('<body>Verify you are human</body>')
            with pytest.raises(CollectorUnavailable,match='restriction'):trademe_search_html(page,root,3000,[])
            page.set_content('<body><h1>Whoops!</h1>Unable to retrieve search results</body>')
            with pytest.raises(CollectorUnavailable,match='temporarily unavailable at the source'):
                trademe_search_html(page,root,3000,[])
        finally:browser.close()
