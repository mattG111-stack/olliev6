import json
import pytest
from models import PortalCollectionRun, PortalObservation, PortalListing, PropertyForSale, PropertySold
from portals.storage import collect_and_stage
from portals.direct import CollectorUnavailable


def sample(**values):
    return dict(source='oneroof',url='https://www.oneroof.co.nz/property/example',kind='for_sale',
        address='25 Example Road',suburb='Example',district='Auckland City',region='Auckland',
        _apex_direct=True,scraped_at='2026-09-25T10:00:00Z',raw_source={'extra':'retained'},**values)


def test_repeated_collection_keeps_evidence_and_refreshes_only_pending(db_session,monkeypatch):
    current=sample(price_numeric=900000)
    monkeypatch.setattr('portals.direct.collect',lambda *a,**k:[current])
    def run():return collect_and_stage(db_session,sources=['oneroof'],kind='for_sale',cap=5)['merged']
    assert run()['new']==1
    current['price_numeric']=850000
    assert run()['refreshed']==1
    pending=db_session.query(PortalListing).one()
    assert pending.price_numeric==850000 and pending.status=='pending'
    current.pop('price_numeric');current['price_display']='By negotiation'
    run()
    assert pending.price_numeric is None
    pending.status='rejected';db_session.commit()
    current['price_numeric']=800000
    assert run()['refreshed']==0
    assert pending.status=='rejected' and pending.price_numeric is None
    assert db_session.query(PortalObservation).count()==4
    first=db_session.query(PortalObservation).order_by(PortalObservation.id).first()
    assert json.loads(first.payload_json)['price_numeric']==900000
    assert db_session.query(PropertyForSale).count()==0
    assert db_session.query(PortalCollectionRun).filter_by(status='complete').count()==4


def test_failure_keeps_completed_source_evidence_but_stages_nothing(db_session,monkeypatch):
    def collect(source,**kw):
        if source=='homes':raise CollectorUnavailable('Example failure with secret that must not be stored')
        return [sample(price_numeric=900000)]
    monkeypatch.setattr('portals.direct.collect',collect)
    with pytest.raises(CollectorUnavailable):collect_and_stage(db_session,sources=['oneroof','homes'],kind='for_sale',cap=5)
    run=db_session.query(PortalCollectionRun).one()
    assert run.status=='failed' and run.error_type=='CollectorUnavailable'
    assert 'secret' not in (run.summary_json or '')
    assert db_session.query(PortalObservation).count()==1
    assert db_session.query(PortalListing).count()==0


def test_undisclosed_sold_price_stored_as_evidence_not_comparable(db_session,monkeypatch):
    row={**sample(),'kind':'sold','sold_date':'2026-09-01','price_display':'TBC'}
    monkeypatch.setattr('portals.direct.collect',lambda *a,**k:[row])
    result=collect_and_stage(db_session,sources=['oneroof'],kind='sold',cap=5)['merged']
    assert result['excluded']==1 and result['new']==0
    assert db_session.query(PortalObservation).count()==1
    assert db_session.query(PropertySold).count()==0


def test_collection_migration_roundtrip_preserves_existing_tables(db_session):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    path=next((Path(__file__).resolve().parents[1]/'alembic'/'versions').glob('c8d9e0f1a2b3*'))
    spec=importlib.util.spec_from_file_location('collection_migration',path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=db_session.get_bind()
    with engine.begin() as conn:
        PortalObservation.__table__.drop(conn)
        PortalCollectionRun.__table__.drop(conn)
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            assert 'portal_observations' in inspect(conn).get_table_names()
            assert inspect(conn).get_foreign_keys('portal_observations')[0]['referred_table']=='portal_collection_runs'
            migration.downgrade()
            assert 'portal_observations' not in inspect(conn).get_table_names()
            assert 'properties_sold' in inspect(conn).get_table_names()
            migration.upgrade()


def test_bootstrap_stamp_matches_current_migration_head():
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from db_bootstrap import HEAD_REVISION
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'alembic'))
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD_REVISION]


def test_checkpoint_commits_only_after_all_sources_and_staging_succeed(db_session,monkeypatch):
    from portals import checkpoints
    def fail_second(source,checkpoint,**kw):
        checkpoint['pending_urls']=['https://www.oneroof.co.nz/next']
        if source=='homes':raise CollectorUnavailable('synthetic failure')
        return [sample(price_numeric=900000)]
    monkeypatch.setattr('portals.direct.collect',fail_second)
    with pytest.raises(CollectorUnavailable):
        collect_and_stage(db_session,sources=['oneroof','homes'],kind='for_sale',cap=5)
    assert checkpoints.load(db_session,'oneroof','for_sale')=={}
    assert db_session.query(PortalListing).count()==0
    def success(source,checkpoint,**kw):
        checkpoint['pending_urls']=['https://www.oneroof.co.nz/next']
        return [sample(price_numeric=900000)]
    monkeypatch.setattr('portals.direct.collect',success)
    collect_and_stage(db_session,sources=['oneroof'],kind='for_sale',cap=5)
    assert checkpoints.load(db_session,'oneroof','for_sale')['pending_urls']==['https://www.oneroof.co.nz/next']
    assert db_session.query(PortalListing).count()==1


def test_checkpoint_write_failure_rolls_back_new_review_rows(db_session,monkeypatch):
    from portals import checkpoints
    monkeypatch.setattr('portals.direct.collect',lambda *a,**kw:[sample(price_numeric=900000)])
    def fail(*a,**kw):raise RuntimeError('synthetic checkpoint write failure')
    monkeypatch.setattr(checkpoints,'save',fail)
    with pytest.raises(RuntimeError,match='checkpoint'):
        collect_and_stage(db_session,sources=['oneroof'],kind='for_sale',cap=5)
    assert db_session.query(PortalListing).count()==0
    assert db_session.query(PortalObservation).count()==1
    assert checkpoints.load(db_session,'oneroof','for_sale')=={}


def test_partial_resume_does_not_restart_already_finished_source(db_session,monkeypatch):
    calls=[]
    def collect(source,checkpoint,**kw):
        calls.append(source)
        if source=='homes' and not checkpoint.get('pending_urls'):
            checkpoint['pending_urls']=['https://homes.co.nz/address/auckland/example/2/test']
        else:
            checkpoint['pending_urls']=[]
            checkpoint['last_completed_pass_at']='2026-09-26T00:00:00Z'
        return [{**sample(price_numeric=900000),'source':source}]
    monkeypatch.setattr('portals.direct.collect',collect)
    def run():return collect_and_stage(db_session,sources=['oneroof','homes'],kind='for_sale',cap=5)
    assert run()['merged']['pending']==1
    assert run()['merged']['pending']==0
    assert calls==['oneroof','homes','homes']
    # Once the whole pass completes, the next scheduled pass checks both again.
    run()
    assert calls[-2:]==['oneroof','homes']


def test_later_source_enriches_same_pending_listing_across_runs(db_session,monkeypatch):
    first=sample(price_numeric=900000,beds=3)
    second={**sample(price_numeric=900000,floor_area_m2=140),'source':'homes',
            'url':'https://homes.co.nz/address/auckland/example/25/abc'}
    monkeypatch.setattr('portals.direct.collect',lambda source,**kw:[first if source=='oneroof' else second])
    collect_and_stage(db_session,sources=['oneroof'],kind='for_sale',cap=5)
    result=collect_and_stage(db_session,sources=['homes'],kind='for_sale',cap=5)['merged']
    pending=db_session.query(PortalListing).all()
    assert len(pending)==1
    assert result['refreshed']==1 and result['new']==0
    assert pending[0].beds==3 and pending[0].floor_area_m2==140
    assert pending[0].source=='oneroof' and pending[0].status=='pending'
    evidence=json.loads(pending[0].raw_json)
    assert {r['source'] for r in evidence['source_snapshots']}=={'oneroof','homes'}
    assert db_session.query(PropertyForSale).count()==0
    assert db_session.query(PortalObservation).count()==2


def test_cross_run_source_refresh_replaces_its_own_old_price(db_session,monkeypatch):
    first=sample(price_numeric=900000,beds=3)
    second={**sample(floor_area_m2=140),'source':'homes',
            'url':'https://homes.co.nz/address/auckland/example/25/abc'}
    monkeypatch.setattr('portals.direct.collect',lambda source,**kw:[first if source=='oneroof' else second])
    for source in ('oneroof','homes'):
        collect_and_stage(db_session,sources=[source],kind='for_sale',cap=5)
    first['price_numeric']=850000
    collect_and_stage(db_session,sources=['oneroof'],kind='for_sale',cap=5)
    row=db_session.query(PortalListing).one()
    assert row.price_numeric==850000 and row.floor_area_m2==140
    evidence=json.loads(row.raw_json)
    assert 'price_numeric' not in evidence['conflicts']
    assert len(evidence['source_snapshots'])==2
    assert db_session.query(PortalObservation).count()==3


@pytest.mark.parametrize('change',[{'address':'2/25 Example Road'},
                                  {'district':'Other district'},
                                  {'region':'Other region'}])
def test_cross_run_enrichment_does_not_join_other_property(db_session,monkeypatch,change):
    first=sample(price_numeric=900000,beds=3)
    second={**sample(floor_area_m2=140),'source':'homes',
            'url':'https://homes.co.nz/address/auckland/example/25/abc',**change}
    monkeypatch.setattr('portals.direct.collect',lambda source,**kw:[first if source=='oneroof' else second])
    for source in ('oneroof','homes'):
        collect_and_stage(db_session,sources=[source],kind='for_sale',cap=5)
    assert db_session.query(PortalListing).count()==2
    assert db_session.query(PortalListing).filter_by(source='oneroof').one().floor_area_m2 is None


def test_latest_runs_reports_each_kind_without_private_evidence(db_session):
    from portals.storage import latest_runs
    db_session.add_all([
        PortalCollectionRun(kind='for_sale',status='failed',summary_json='{}'),
        PortalCollectionRun(kind='sold',status='complete',summary_json=json.dumps({
            'oneroof':{'observed':3,'secret':'private'},'homes':{'observed':True},
            'trademe':{'observed':-5},'merged':{'new':2,'pending':4,'excluded':1,
            'refreshed':False,'discovery_pending':'private'},'raw':'private'})),
        PortalCollectionRun(kind='for_sale',status='running',summary_json='broken-json'),
        PortalCollectionRun(kind='rental',status='complete',summary_json='{}')])
    db_session.commit()
    result=latest_runs(db_session)
    assert len(result)==2
    assert result[0]['kind']=='for_sale' and result[0]['status']=='running'
    assert result[0]['observed']==0
    assert result[1]['observed']==3 and result[1]['new']==2
    assert result[1]['pending']==4 and result[1]['excluded']==1
    assert 'refreshed' not in result[1] and 'discovery_pending' not in result[1]
    assert 'private' not in json.dumps(result)


def test_latest_runs_empty_and_non_object_summary(db_session):
    from portals.storage import latest_runs
    assert latest_runs(db_session)==[]
    db_session.add(PortalCollectionRun(kind='sold',status='complete',summary_json='[]'))
    db_session.commit()
    assert latest_runs(db_session)[0]['observed']==0


@pytest.mark.parametrize("job,collector", [("sweep_new_listings","sweep"),("sweep_sold_listings","sweep_sold")])
def test_daily_wrapper_returns_durable_summary(db_session,monkeypatch,job,collector):
    from portals import daily
    summary={'oneroof':{'observed':3},'merged':{'new':2,'pending':4,'discovery_pending':1}}
    monkeypatch.setattr(daily,'SessionLocal',lambda:db_session)
    monkeypatch.setattr('portals.listings.'+collector,lambda db:summary)
    assert getattr(daily,job)()==summary
