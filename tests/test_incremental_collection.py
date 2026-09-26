import pytest
from datetime import datetime, timezone
from portals.incremental import start, stop_after_page, finish

NOW=datetime(2026,9,26,tzinfo=timezone.utc)
SEED='https://www.oneroof.co.nz/search/houses-for-sale/region_auckland-35_order_latest-0_page_1'


def state():
    return {'last_full_pass_at':'2026-09-25T00:00:00+00:00',
            'last_completed_pass_at':'2026-09-25T00:00:00+00:00'}


def test_overlap_requires_two_whole_old_pages_and_unknown_date_resets_boundary():
    s=state();start(s,'oneroof','for_sale',[SEED],now=NOW)
    assert s['recent_cutoff']=='2026-09-18'
    old=[{'kind':'for_sale','listed_date':'2026-09-01'}]
    assert not stop_after_page(s,old)
    assert not stop_after_page(s,[{'kind':'for_sale','listed_date':None}])
    assert not stop_after_page(s,old)
    assert stop_after_page(s,old)


def test_full_scan_for_initial_weekly_sold_and_unsorted_searches():
    cases=[({},'oneroof','for_sale',SEED),
           ({**state(),'last_full_pass_at':'2026-09-01T00:00:00+00:00'},'oneroof','for_sale',SEED),
           (state(),'oneroof','sold',SEED),
           (state(),'oneroof','for_sale',SEED.replace('_order_latest-0','')),
           (state(),'homes','for_sale','https://homes.co.nz/map')]
    for s,source,kind,seed in cases:
        start(s,source,kind,[seed],now=NOW)
        assert s['recent_cutoff'] is None


def test_resume_preserves_boundary_until_saved_details_are_complete():
    s=state();start(s,'oneroof','for_sale',[SEED],now=NOW)
    s.update(pending_urls=['https://www.oneroof.co.nz/property/example'],recent_old_pages=1)
    start(s,'oneroof','for_sale',[SEED],now=NOW)
    assert s['recent_old_pages']==1 and s['recent_cutoff']=='2026-09-18'


def test_verified_trademe_sort_supported_but_price_sort_is_not():
    for order,expected in [('expirydesc','2026-09-18'),('priceasc',None)]:
        s=state();start(s,'trademe','for_sale',[
            'https://www.trademe.co.nz/a/property/residential/sale/auckland/search?sort_order='+order],now=NOW)
        assert s['recent_cutoff']==expected


def test_collector_stops_search_after_overlap_and_retains_unread_details(monkeypatch):
    import json
    from config import settings
    from portals.direct import collect,page_data
    from portals import incremental
    original=incremental.start
    monkeypatch.setattr(incremental,'start',lambda *a:original(*a,now=NOW))
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'oneroof':{'for_sale':[SEED]}}))
    monkeypatch.setattr(settings,'scraper_max_pages',2)
    second=SEED[:-1]+'2';third=SEED[:-1]+'3'
    details=['https://www.oneroof.co.nz/property/first','https://www.oneroof.co.nz/property/second']
    monkeypatch.setattr(page_data,'extract',lambda html,source:{details[0 if second in html else 1]:{}})
    monkeypatch.setattr(page_data,'normalise',lambda *a:{'address':'1 Example Road',
        'region':'Auckland','listed_date':'2026-09-01','scraped_at':NOW.isoformat()})
    calls=[]
    class Pages:
        def get(self,url):
            calls.append(url)
            assert url!=third
            return '<a href="'+(second if url==SEED else third)+'">Next</a>'
    s=state()
    assert len(collect('oneroof',transport=Pages(),checkpoint=s))==2
    assert calls==[SEED,second]
    assert s['pending_urls']==details
    assert s['last_full_pass_at']=='2026-09-25T00:00:00+00:00'


def test_next_cutoff_uses_scan_start_so_changes_during_long_run_are_revisited():
    s={};start(s,'oneroof','for_sale',[SEED],now=NOW)
    finish(s,'2026-09-28T00:00:00+00:00')
    assert s['last_successful_scan_started_at']==NOW.isoformat()
    start(s,'oneroof','for_sale',[SEED],now=datetime(2026,9,29,tzinfo=timezone.utc))
    assert s['recent_cutoff']=='2026-09-19'


def test_unfinished_or_partial_discovery_never_advances_successful_watermark():
    for extra in ({'pending_urls':['https://www.oneroof.co.nz/next']},
                  {'discovery_coverage':{'loaded':60,'total':4943,'complete':False}}):
        s={'scan_started_at':NOW.isoformat(),**extra}
        finish(s,NOW.isoformat())
        assert 'last_successful_scan_started_at' not in s
        assert 'last_full_pass_at' not in s



@pytest.mark.parametrize("persist", [False, True])
def test_undisclosed_sale_is_revisited_when_it_disappears_from_search(monkeypatch, db_session, persist):
    from portals.direct import collect, page_data
    from config import settings
    import json
    seed = 'https://www.oneroof.co.nz/search/sold/example'
    detail = 'https://www.oneroof.co.nz/property/example'
    newer = 'https://www.oneroof.co.nz/property/newer'
    monkeypatch.setattr(settings, 'scraper_seeds', json.dumps({'oneroof':{'sold':[seed]}}))
    monkeypatch.setattr(settings, 'scraper_max_pages', 4)
    phase = [0]; calls = []
    class Pages:
        def get(self, url): calls.append(url); return url
        def close(self): pass
    def extract(url, source):
        target = (detail if phase[0] == 0 else newer) if url == seed else url
        return {target: {'target':target}}
    monkeypatch.setattr(page_data, 'extract', extract)
    monkeypatch.setattr(page_data, 'normalise', lambda source, kind, url, raw: {
        'address':'1 Example Road' if url==detail else '2 Example Road',
        'region':'Auckland', 'suburb':'Example', 'district':'Auckland City',
        'scraped_at':'2026-09-26', 'sold_date':'2026-09-01',
        'sale_price':900000 if phase[0] and url==detail else None})
    from portals.storage import collect_and_stage
    from portals import checkpoints
    from models import PropertySold
    monkeypatch.setattr('portals.direct.Transport', lambda source: Pages())
    state = {}
    if persist:
        collect_and_stage(db_session,sources=['oneroof'],kind='sold',cap=5)
        state = checkpoints.load(db_session,'oneroof','sold')
        assert db_session.query(PropertySold).count() == 0
    else:
        collect('oneroof',kind='sold',transport=Pages(),checkpoint=state)
    assert state['sold_recheck_urls'] == [detail]
    phase[0] = 1; calls.clear()
    if persist:
        collect_and_stage(db_session,sources=['oneroof'],kind='sold',cap=5)
        state = checkpoints.load(db_session,'oneroof','sold')
        assert db_session.query(PropertySold).one().sale_price == 900000
    else:
        rows = collect('oneroof',kind='sold',transport=Pages(),checkpoint=state)
        assert next(r for r in rows if r['url']==detail)['sale_price'] == 900000
    assert detail in calls and seed in calls
    assert state['sold_recheck_urls'] == [newer]


def test_untrusted_sold_recheck_url_cannot_leave_source(monkeypatch):
    from portals.direct import collect, CollectorUnavailable
    from config import settings
    import json
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'homes':{'sold':['https://homes.co.nz/map/auckland']}}))
    class NoRequests:
        def get(self,url): raise AssertionError('must validate before requesting')
    with pytest.raises(CollectorUnavailable):
        collect('homes',kind='sold',transport=NoRequests(),checkpoint={'sold_recheck_urls':['http://127.0.0.1/private']})
