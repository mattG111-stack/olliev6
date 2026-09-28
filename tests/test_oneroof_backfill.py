import json
import pytest
from tests.test_portal_complete import _pending
from portals import oneroof_backfill as B
from portals.complete import fill_pending
URL='https://www.oneroof.co.nz/property/auckland/westgate/example'
def item(**kw):
    data=dict(_apex_direct=True,source='oneroof',url=URL,address='24 Lomandra Street',suburb='Westgate',scraped_at='2026-09-28T00:00:00+00:00',floor_area_m2=149.0,land_area_m2=405.0,beds=4,baths=2,cv_numeric=1050000.0,images=['https://images.example.nz/a.jpg'],provenance={},raw_source={'retained':'source payload'})
    data.update(kw)
    return data

def test_fill_missing_uses_oneroof_without_pv(db_session,monkeypatch):
    row=_pending(db_session,url=URL,raw_json=json.dumps({'_apex_direct':True,'scraped_at':'original'}))
    monkeypatch.setattr(B,'fetch',lambda url:item())
    monkeypatch.setattr('portals.complete.pv_lookup_status',lambda *a:pytest.fail('PV should not run'))
    out=fill_pending(db_session)
    assert out['fields_filled']>0 and row.land_area_m2==405 and row.image_count==1
    raw=json.loads(row.raw_json)
    assert raw['scraped_at']=='original' and raw['oneroof_backfill']['evidence']['raw_source']
    assert raw['backfill_provenance']['land_area_m2']['source']=='oneroof'
    assert row.status=='pending' and row.price_numeric==900000

@pytest.mark.parametrize('address,suburb',[('2/24 Lomandra Street','Westgate'),('24 Lomandra Street','Other'),('24-26 Lomandra Street','Westgate')])
def test_rejects_wrong_unit_suburb_and_range(db_session,address,suburb):
    row=_pending(db_session,url=URL)
    assert B.apply(row,item(address=address,suburb=suburb))==(0,'identity_mismatch')
    assert row.land_area_m2 is None

def test_conflict_is_retained(db_session):
    row=_pending(db_session,url=URL,floor_area_m2=150)
    B.apply(row,item())
    assert row.floor_area_m2==150
    assert json.loads(row.raw_json)['source_conflicts']['floor_area_m2']==[150,149.0]

def test_internal_conflict_not_filled(db_session):
    row=_pending(db_session,url=URL,floor_area_m2=None)
    B.apply(row,item(source_conflicts={'floor_area_m2':[149,200]}))
    assert row.floor_area_m2 is None
    assert json.loads(row.raw_json)['source_conflicts']['floor_area_m2']==[149,200]

def test_other_source_resolves_exact_link(db_session):
    _pending(db_session,url=URL,address_key=B.address_key('24 Lomandra Street','Westgate'))
    row=_pending(db_session,source='homes',url='https://homes.co.nz/address/example')
    assert B.known_url(db_session,row)==URL
    row.address='2/24 Lomandra Street'
    assert B.known_url(db_session,row) is None

def test_fetch_rejects_recommended_neighbour(monkeypatch):
    class Fake:
        resolved_urls={}
        def get(self,url):return 'html'
        def close(self):pass
    monkeypatch.setattr(B.direct,'Transport',lambda *a:Fake())
    monkeypatch.setattr(B.page_data,'extract',lambda *a:{URL+'/neighbour':{}})
    assert B.fetch(URL) is None

def test_sold_not_modified(db_session):
    row=_pending(db_session,url=URL,kind='sold')
    assert B.fill(db_session,row) is None


def test_source_failure_pauses_following_rows(db_session,monkeypatch):
    row=_pending(db_session,url=URL)
    calls=[]
    def fail(url):
        calls.append(url)
        raise B.direct.CollectorUnavailable('Source paused after access restriction')
    monkeypatch.setattr(B,'fetch',fail)
    assert B.fill(db_session,row)==(0,'paused')
    db_session.commit()
    assert B.fill(db_session,row)==(0,'paused')
    assert len(calls)==1


def test_oneroof_building_decade_is_preserved():
    raw={'street':'24 Lomandra Street','suburb':'Westgate','buildingAge':'1950s'}
    result=B.direct.canonical('oneroof','for_sale',URL,raw)
    assert result['building_age']=='1950s'
    assert B.to_listing('oneroof',result)['building_age']=='1950s'


def test_source_failure_pauses_same_uncommitted_chunk(db_session, monkeypatch):
    row = _pending(db_session, url=URL)
    db_session.autoflush = False
    calls = []
    def fail(url):
        calls.append(url)
        raise B.direct.CollectorUnavailable('Source unavailable')
    monkeypatch.setattr(B, 'fetch', fail)
    for _ in range(25):
        assert B.fill(db_session, row) == (0, 'paused')
    db_session.commit()
    assert len(calls) == 1
    assert db_session.query(B.AppSetting).filter_by(
        key='scraper.backfill.oneroof.retry_after').count() == 1


@pytest.mark.parametrize('values,expected', [(['2021','2020s'],True),(['1930','1930s'],True),(['2010','2020s'],False),(['2021','2022','2020s'],False),(['unknown','2020s'],False),(['2026','2020s'],True)])
def test_age_evidence_precision(values,expected):
    from portals.age_evidence import compatible
    assert compatible(values) is expected


def test_compatible_age_backfill_preserves_exact_year(db_session):
    row=_pending(db_session,url=URL,building_age='2021')
    B.apply(row,item(building_age='2020s'))
    assert row.building_age=='2021'
    assert 'building_age' not in json.loads(row.raw_json)['source_conflicts']


def test_existing_age_hold_resolution_archives_evidence(db_session):
    from portals.age_evidence import resolve_compatible_age
    flag='Source records disagree — review source evidence before approval'
    row=_pending(db_session,url=URL,building_age='2021',price_flag=flag,raw_json=json.dumps({'building_age':'2021','source_conflicts':{'building_age':['2021','2020s']},'source_snapshots':[{'building_age':'2020s'}]}))
    assert resolve_compatible_age(row)
    raw=json.loads(row.raw_json)
    assert row.price_flag is None and row.building_age=='2021'
    assert raw['compatible_age_resolutions'][0]['evidence']==['2021','2020s']
    assert raw['source_snapshots']==[{'building_age':'2020s'}]
    assert not resolve_compatible_age(row)


def test_age_resolution_preserves_other_conflicts(db_session):
    from portals.age_evidence import resolve_compatible_age
    row=_pending(db_session,url=URL,building_age='2021',price_flag='Source records disagree — review source evidence before approval',raw_json=json.dumps({'source_conflicts':{'building_age':['2021','2020s'],'floor_area_m2':[100,150]}}))
    assert resolve_compatible_age(row)
    assert row.price_flag and json.loads(row.raw_json)['source_conflicts']['floor_area_m2']==[100,150]


def test_pause_preserves_cursor_and_resumes_same_property(db_session, monkeypatch):
    from models import AppSetting
    row = _pending(db_session, url=URL)
    calls = []
    def fail(url):
        calls.append(url)
        raise B.direct.CollectorUnavailable('source unavailable')
    monkeypatch.setattr(B, 'fetch', fail)
    first = fill_pending(db_session)
    assert first['paused'] and first['scanned'] == 0
    assert first['last_id'] == 0 and first['looked_up'] == 0
    second = fill_pending(db_session, after_id=first['last_id'])
    assert second['paused'] and calls == [URL]
    cooldown = db_session.get(AppSetting, 'scraper.backfill.oneroof.retry_after')
    cooldown.value = '2000-01-01T00:00:00+00:00'
    db_session.commit()
    monkeypatch.setattr(B, 'fetch', lambda url: item())
    resumed = fill_pending(db_session, after_id=second['last_id'])
    assert not resumed.get('paused') and resumed['last_id'] == row.id
    assert resumed['fields_filled'] > 0
