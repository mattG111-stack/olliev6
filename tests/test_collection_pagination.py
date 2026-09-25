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
