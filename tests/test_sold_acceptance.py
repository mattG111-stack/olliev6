import pytest
from models import PropertySold, PortalObservation, PortalCollectionRun
from portals.storage import collect_and_stage


def sale(**kw):
    return dict(source='oneroof',kind='sold',url='https://www.oneroof.co.nz/property/example',
        _apex_direct=True,address='1 Example Road',suburb='Example',district='Auckland City',region='Auckland',
        sold_date='2026-01-03',sale_price=900000,**kw)


def run(db,monkeypatch,rows):
    monkeypatch.setattr('portals.direct.collect',lambda *a,**k:rows)
    return collect_and_stage(db,sources=['oneroof'],kind='sold',cap=5)['merged']


def test_valid_sale_saves_without_approval_and_dedupes_exact_transaction(db_session,monkeypatch):
    assert run(db_session,monkeypatch,[sale()])['new']==1
    assert db_session.query(PropertySold).one().sale_price==900000
    assert run(db_session,monkeypatch,[sale()])['skipped']==1
    second={**sale(),'sold_date':'2026-01-25'}
    assert run(db_session,monkeypatch,[second])['new']==1
    assert db_session.query(PropertySold).count()==2
    assert db_session.query(PortalObservation).count()==3


@pytest.mark.parametrize('change',[{'sale_price':None},{'sale_price':-1},{'sale_price':float('nan')},{'sold_date':'2099-01-01'},{'sold_date':'not a date'},{'conflicts':{'sale_price':[{'value':800000}]}}])
def test_invalid_or_conflicting_sale_never_enters_pricing(db_session,monkeypatch,change):
    result=run(db_session,monkeypatch,[{**sale(),**change}])
    assert result['quarantined']==1
    assert db_session.query(PropertySold).count()==0
    assert db_session.query(PortalObservation).count()==1


def test_changed_price_for_same_transaction_is_not_a_new_comparable(db_session,monkeypatch):
    run(db_session,monkeypatch,[sale()])
    result=run(db_session,monkeypatch,[{**sale(),'sale_price':800000}])
    assert result['quarantined']==1
    assert db_session.query(PropertySold).one().sale_price==900000


def test_legacy_csv_date_and_iso_date_are_same_transaction(db_session,monkeypatch):
    run(db_session,monkeypatch,[sale()])
    stored=db_session.query(PropertySold).one();stored.sold_date='1/3/2026';db_session.commit()
    assert run(db_session,monkeypatch,[sale()])['skipped']==1
    assert db_session.query(PropertySold).count()==1


def test_partial_promotion_rolls_back_on_failure(db_session,monkeypatch):
    from portals import listings
    original=listings._portal_sold_batch
    calls=[]
    def failing(db):
        calls.append(1)
        if len(calls)==2:raise RuntimeError('synthetic failure')
        return original(db)
    monkeypatch.setattr(listings,'_portal_sold_batch',failing)
    with pytest.raises(RuntimeError):run(db_session,monkeypatch,[sale(),{**sale(),'address':'2 Example Road'}])
    assert db_session.query(PropertySold).count()==0
    assert db_session.query(PortalObservation).count()==2
    assert db_session.query(PortalCollectionRun).one().status=='failed'


def test_conflicting_valid_sale_is_visible_in_review(db_session,monkeypatch):
    from models import PortalListing
    item=sale(conflicts={'sale_price':[{'value':800000}]})
    result=run(db_session,monkeypatch,[item])
    assert result['quarantined']==1
    pending=db_session.query(PortalListing).one()
    assert pending.status=='pending' and pending.price_flag
    assert db_session.query(PropertySold).count()==0
    run(db_session,monkeypatch,[item])
    assert db_session.query(PortalListing).count()==1


def test_different_portal_property_ids_do_not_dispute_an_agreed_sale(db_session,monkeypatch):
    import json
    from models import PortalListing
    first = sale(property_id='oneroof-property-101')
    second = {**sale(property_id='homes-property-202'), 'source':'homes',
              'url':'https://homes.co.nz/address/auckland/example/1/abc'}
    result = run(db_session,monkeypatch,[first,second])
    assert result['new']==1 and result['quarantined']==0
    assert db_session.query(PropertySold).one().sale_price==900000
    evidence=json.loads(db_session.query(PortalListing).one().raw_json)
    assert {r['property_id'] for r in evidence['source_snapshots']} == {
        'oneroof-property-101','homes-property-202'}


def test_later_source_fills_missing_sale_facts_and_preserves_evidence(db_session,monkeypatch):
    import json
    from models import PortalListing
    run(db_session,monkeypatch,[sale(beds=3)])
    later={**sale(beds=3,floor_area_m2=140,building_age=2019),'source':'homes',
           'url':'https://homes.co.nz/address/auckland/example/1/abc'}
    result=run(db_session,monkeypatch,[later])
    assert result['enriched']==1 and result['new']==0
    stored=db_session.query(PropertySold).one()
    assert stored.floor_area_m2==140 and stored.building_age=='2019'
    assert stored.sale_price==900000 and stored.sold_date=='2026-01-03'
    evidence=json.loads(db_session.query(PortalListing).filter_by(status='approved').one().raw_json)
    assert {s['source'] for s in evidence['source_snapshots']}=={'oneroof','homes'}
    assert db_session.query(PortalObservation).count()==2


def test_later_conflicting_fact_quarantines_without_partial_enrichment(db_session,monkeypatch):
    run(db_session,monkeypatch,[sale(beds=3)])
    result=run(db_session,monkeypatch,[{**sale(beds=4,floor_area_m2=140),
        'source':'homes','url':'https://homes.co.nz/address/auckland/example/1/abc'}])
    assert result['quarantined']==1 and result['enriched']==0
    stored=db_session.query(PropertySold).one()
    assert stored.beds==3 and stored.floor_area_m2 is None


def test_repeated_same_source_facts_do_not_expand_approved_evidence(db_session,monkeypatch):
    import json
    from models import PortalListing
    run(db_session,monkeypatch,[sale(building_age='2019',beds=3)])
    for stamp in ('2026-09-25','2026-09-26'):
        result=run(db_session,monkeypatch,[sale(building_age='2019',beds=3,scraped_at=stamp)])
        assert result['skipped']==1 and result['quarantined']==0
    evidence=json.loads(db_session.query(PortalListing).one().raw_json)
    assert len(evidence['source_snapshots'])==1
    assert db_session.query(PortalObservation).count()==3


def test_different_unit_or_sale_date_never_enriches_original_sale(db_session,monkeypatch):
    run(db_session,monkeypatch,[{**sale(),'address':'1/10 Example Road'}])
    for change in ({'address':'2/10 Example Road'}, {'address':'1/10 Example Road','sold_date':'2026-02-03'}):
        result=run(db_session,monkeypatch,[{**sale(floor_area_m2=140),**change}])
        assert result['new']==1 and result['enriched']==0
    original=db_session.query(PropertySold).filter_by(address='1/10 Example Road',sold_date='2026-01-03').one()
    assert original.floor_area_m2 is None


def test_untracked_existing_sale_is_not_rewritten_by_enrichment(db_session,monkeypatch):
    from models import PortalListing
    run(db_session,monkeypatch,[sale()])
    db_session.query(PortalListing).delete();db_session.commit()
    result=run(db_session,monkeypatch,[sale(floor_area_m2=140)])
    assert result['skipped']==1 and result['enriched']==0
    assert db_session.query(PropertySold).one().floor_area_m2 is None
