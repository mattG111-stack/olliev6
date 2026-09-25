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
