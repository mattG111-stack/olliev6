from portals.direct import next_pages


def test_oneroof_only_next_visible_page_same_search():
    url='https://www.oneroof.co.nz/search/sold/region_auckland-35_page_1'
    html=''.join(f'<a href="{href}">Page</a>' for href in [
        '/search/sold/region_auckland-35_page_2',
        '/search/sold/region_auckland-35_page_20',
        '/search/houses-for-sale/region_auckland-35_page_2'])
    assert next_pages(html,url,'oneroof')==[url[:-1]+'2']


def test_trademe_aria_next_preserves_filters_and_does_not_follow_previous():
    url='https://www.trademe.co.nz/a/property/insights/search/auckland/mount-wellington?bedrooms=3&page=3'
    html=''.join(f'<a aria-label="Next page, page {n}" href="?{query}">Next</a>' for n,query in [
        (2,'bedrooms=3&page=2'),(4,'bedrooms=3&page=4'),(4,'bedrooms=2&page=4'),(4,'page=4')])
    assert next_pages(html,url,'trademe')==[url[:-1]+'4']


def test_search_pagination_is_not_starved_by_detail_enrichment(monkeypatch):
    import json
    from config import settings
    from portals.direct import collect, page_data
    first='https://www.oneroof.co.nz/search/sold/region_auckland-35_page_1'
    second=first[:-1]+'2'
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'oneroof':{'sold':[first]}}))
    monkeypatch.setattr(settings,'scraper_max_pages',2)
    urls=['https://www.oneroof.co.nz/property/example-'+str(i) for i in range(3)]
    records=[{'address':'1 Example Road','suburb':'Example','region':'Auckland'},
             {'address':'2 Example Road','suburb':'Example','region':'Auckland'},
             {'address':'3 Example Road','suburb':'Example','region':'Auckland'}]
    monkeypatch.setattr(page_data,'extract',lambda html,source:dict(zip(urls[:2],records[:2])) if 'Next' in html else {urls[2]:records[2]})
    monkeypatch.setattr(page_data,'normalise',lambda source,kind,url,raw:{**raw,'scraped_at':'2026-09-26T00:00:00Z'})
    requests=[]
    class Pages:
        def get(self,url):
            requests.append(url)
            return '<a href="'+second+'">Next</a>' if url==first else 'second'
    rows=collect('oneroof',kind='sold',transport=Pages())
    assert requests==[first,second]
    assert len(rows)==3
    assert rows[0]['collection_scope']['pending_detail_urls']==urls
    assert not rows[0]['collection_scope']['complete_coverage_verified']


def test_resume_reads_saved_pending_url_before_starting_new_pass(monkeypatch):
    import json
    from config import settings
    from portals.direct import collect,page_data
    seed='https://homes.co.nz/map'
    detail='https://homes.co.nz/address/auckland/example/1/abc'
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'homes':{'sold':[seed]}}))
    monkeypatch.setattr(page_data,'extract',lambda html,source:{detail:{'_apex_kind':'sold'}})
    monkeypatch.setattr(page_data,'normalise',lambda *a:{'address':'1 Example Road','suburb':'Example','region':'Auckland','scraped_at':'2026-09-26'})
    calls=[]
    class Pages:
        def get(self,url):calls.append(url);return 'detail'
    state={'pending_urls':[detail]}
    assert len(collect('homes',kind='sold',transport=Pages(),checkpoint=state))==1
    assert calls==[detail]
    assert state['pending_urls']==[] and state['last_completed_pass_at']


def test_capped_search_preserves_disclosed_prices_across_checkpoint_restart(monkeypatch):
    import json
    from config import settings
    from portals.direct import collect,page_data
    seed='https://www.oneroof.co.nz/search/sold/region_auckland-35_page_1'
    urls=['https://www.oneroof.co.nz/property/example-'+str(i) for i in range(3)]
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'oneroof':{'sold':[seed]}}))
    monkeypatch.setattr(settings,'scraper_max_pages',1)
    records=[dict(address=f'{i} Example Road',suburb='Example',region='Auckland',
                  sale_price=900000+i,sold_date='2026-06-01') for i in range(3)]
    def extract(html,source):
        if html==seed:return dict(zip(urls,records))
        row=dict(records[urls.index(html)]);row.pop('sale_price')
        return {html:row}
    monkeypatch.setattr(page_data,'extract',extract)
    monkeypatch.setattr(page_data,'normalise',lambda source,kind,url,raw:{**raw,'scraped_at':'2026-09-26T00:00:00Z'})
    calls=[]
    class Pages:
        def get(self,url):calls.append(url);return url
    state={};all_rows=[]
    for _ in range(10):
        all_rows.extend(collect('oneroof',kind='sold',cap=1,transport=Pages(),checkpoint=state))
        state=json.loads(json.dumps(state))  # Simulate a fresh worker process.
        if not state['pending_urls']:break
    assert {row['address']:row.get('sale_price') for row in all_rows}=={f'{i} Example Road':900000+i for i in range(3)}
    assert not state['pending_urls']
    assert not state.get('pending_search_records')
    assert calls==[seed,*urls]


def test_resumed_detail_price_conflict_is_not_accepted_as_comparable(monkeypatch):
    import json
    from config import settings
    from portals.direct import collect, page_data
    from portals.sold_acceptance import reason
    seed='https://www.oneroof.co.nz/search/sold/region_auckland-35_page_1'
    detail='https://www.oneroof.co.nz/property/example-conflict'
    search=dict(address='1 Example Road',suburb='Example',region='Auckland',
                sale_price=900000,sold_date='2026-06-01')
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'oneroof':{'sold':[seed]}}))
    monkeypatch.setattr(page_data,'extract',lambda html,source:{detail:{**search,'sale_price':950000}})
    monkeypatch.setattr(page_data,'normalise',lambda source,kind,url,raw:{**raw,'scraped_at':'2026-09-26T00:00:00Z'})
    class Pages:
        def get(self,url):return 'detail'
    state=json.loads(json.dumps({'pending_urls':[detail],'pending_search_records':{detail:search}}))
    row,=collect('oneroof',kind='sold',transport=Pages(),checkpoint=state)
    assert row['source_conflicts']['sale_price']==[900000,950000]
    assert row['raw_source']['search']['sale_price']==900000
    assert row['raw_source']['detail']['sale_price']==950000
    assert reason(row)=='Source evidence requires review'
    assert not state['pending_search_records']


def test_same_listing_small_map_pin_variation_retains_evidence_without_sale_conflict():
    from portals.direct import merge_detail_evidence
    from portals.sold_acceptance import reason
    search=dict(source='oneroof',url='https://www.oneroof.co.nz/property/example',
                address='1 Example Road',suburb='Example',kind='sold',
                sold_date='2026-06-01',sale_price=900000,
                latitude=-36.9742573,longitude=174.8434141)
    detail={**search,'latitude':-36.974211,'longitude':174.843326}
    row=merge_detail_evidence({**search,'raw_source':dict(search),'provenance':{}},
                              {**detail,'raw_source':dict(detail),'provenance':{}},detail)
    assert reason(row) is None
    assert row['raw_source']['search']['latitude']==search['latitude']
    assert row['raw_source']['detail']['latitude']==detail['latitude']


def test_map_pin_tolerance_never_hides_price_address_or_large_coordinate_conflicts():
    from portals.direct import merge_detail_evidence
    base=dict(source='oneroof',url='https://www.oneroof.co.nz/property/example',
              address='1/2 Example Road',suburb='Example',sale_price=900000,
              latitude=-36.9742573,longitude=174.8434141)
    for change,expected in [({'latitude':-36.98},'latitude'),
                            ({'latitude':-36.974211,'sale_price':950000},'sale_price'),
                            ({'latitude':-36.974211,'address':'2/2 Example Road'},'latitude'),
                            ({'latitude':-36.974211,'url':'https://www.oneroof.co.nz/property/other'},'latitude'),
                            ({'latitude':-36.974211,'longitude':None},'latitude')]:
        detail={**base,**change}
        row=merge_detail_evidence({**base,'raw_source':dict(base),'provenance':{}},
                                  {**detail,'raw_source':dict(detail),'provenance':{}},detail)
        assert expected in row['source_conflicts']


def test_resumed_detail_does_not_queue_recommended_properties(monkeypatch):
    import json
    from config import settings
    from portals.direct import collect, page_data
    detail='https://www.oneroof.co.nz/property/example-target'
    recommendation='https://www.oneroof.co.nz/property/recommended-neighbour'
    raw=dict(address='1 Example Road',suburb='Example',region='Auckland',
             sale_price=900000,sold_date='2026-06-01')
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'oneroof':{'sold':[detail]}}))
    monkeypatch.setattr(page_data,'extract',lambda html,source:{recommendation:{**raw,'address':'2 Example Road'},detail:raw})
    monkeypatch.setattr(page_data,'normalise',lambda source,kind,url,raw:{**raw,'scraped_at':'2026-09-26T00:00:00Z'})
    class Pages:
        def get(self,url):return 'detail'
    state={'pending_urls':[detail],'pending_search_records':{detail:raw}}
    rows=collect('oneroof',kind='sold',cap=1,transport=Pages(),checkpoint=state)
    assert [row['url'] for row in rows]==[detail]
    assert not state['pending_urls']
    assert not state['pending_search_records']
