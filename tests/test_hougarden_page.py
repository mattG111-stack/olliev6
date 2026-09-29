import json
from portals.hougarden_page import parse
URL='https://www.hougarden.com/nz/property/auckland/otahuhu/9-33-hutton-street/NukOr'
def page(url=URL):
    data={'url':url,'address':{'streetAddress':'9/33 Hutton Street','addressLocality':'Otahuhu'},'floorSize':{'unitCode':'MTK','value':'60'},'numberOfBedrooms':2}
    return '<script type="application/ld+json">'+json.dumps(data)+'</script><section><h3>Key Facts</h3><div><span>Type of title</span><span>Cross-Lease</span></div><div><span>Decade of construction</span><span>1974</span></div></section>'
def test_exact_page_facts_without_invented_land():
    item=parse(page(),URL)
    assert item['floor_area_m2']==60 and item['beds']==2
    assert item['building_age']=='1974' and item['type_of_title']=='Cross-Lease'
    assert 'land_area_m2' not in item
    assert item['source']=='hougarden'
def test_other_property_schema_rejected():
    assert parse(page(URL+'other'),URL) is None
def test_ambiguous_residences_rejected():
    assert parse(page()+page(),URL) is None


def test_missing_page_does_not_pause_entire_source(db_session, monkeypatch):
    from portals import hougarden_backfill as source, direct
    from models import PortalListing, AppSetting
    row = PortalListing(source='hougarden', kind='for_sale', address='224 Logan Road', suburb='Pukekawa', url=URL)
    monkeypatch.setattr(source, 'known_url', lambda *args: URL)
    def missing(url):
        raise direct.CollectorUnavailable('Source returned HTTP 404')
    monkeypatch.setattr(source, 'fetch', missing)
    assert source.fill(db_session, row) == (0, 'not_found')
    assert db_session.get(AppSetting, 'scraper.backfill.hougarden.retry_after') is None


def test_access_restriction_still_pauses(db_session, monkeypatch):
    from portals import hougarden_backfill as source, direct
    from models import PortalListing, AppSetting
    row = PortalListing(source='hougarden', kind='for_sale', address='224 Logan Road', suburb='Pukekawa', url=URL)
    monkeypatch.setattr(source, 'known_url', lambda *args: URL)
    def blocked(url):
        raise direct.CollectorUnavailable('Source paused: HTTP 403; no proxy retry')
    monkeypatch.setattr(source, 'fetch', blocked)
    assert source.fill(db_session, row) == (0, 'paused')
    assert db_session.get(AppSetting, 'scraper.backfill.hougarden.retry_after') is not None
