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
