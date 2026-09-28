from datetime import datetime,timedelta,timezone
import pytest
from portals.schedule import run_due


def test_restart_does_not_repeat_successful_daily_run(db_session):
    engine=db_session.get_bind();calls=[];now=datetime(2026,9,26,tzinfo=timezone.utc)
    fn=lambda:calls.append(1)
    assert run_due(engine,'sold',86400,fn,now=now)
    assert not run_due(engine,'sold',86400,fn,now=now+timedelta(hours=1))
    assert run_due(engine,'sold',86400,fn,now=now+timedelta(days=1))
    assert len(calls)==2


def test_failure_not_marked_success_and_lock_released(db_session):
    engine=db_session.get_bind()
    with pytest.raises(RuntimeError):run_due(engine,'failed',86400,lambda:{})
    assert run_due(engine,'failed',86400,lambda:None)


def test_second_worker_does_not_enter_running_job(db_session):
    engine=db_session.get_bind();calls=[]
    def outer():
        assert not run_due(engine,'same',86400,lambda:calls.append('duplicate'))
    assert run_due(engine,'same',86400,outer)
    assert not calls


def test_dedicated_scraper_worker_excludes_legacy_live_maintenance():
    from scraper_worker import jobs
    assert {job.name for job in jobs()}=={'new listings sweep','sold sweep','daily validated pricing','listing availability','resume private pricing','requested listing backfill'}


def test_unfinished_saved_pass_resumes_without_waiting_a_day(db_session):
    engine=db_session.get_bind();calls=[];now=datetime(2026,9,26,tzinfo=timezone.utc)
    def partial():calls.append(1);return {'merged':{'pending':2}}
    assert run_due(engine,'partial',86400,partial,now=now)
    assert run_due(engine,'partial',86400,partial,now=now+timedelta(minutes=5))
    assert len(calls)==2


def test_killed_postgres_worker_rolls_back_and_releases_lock(db_session, tmp_path):
    """A real process death must not leave a success stamp or lock behind."""
    import subprocess
    import sys
    import time
    from pathlib import Path
    from models import AppSetting
    engine = db_session.get_bind()
    if engine.dialect.name != 'postgresql':
        pytest.skip('Requires the isolated PostgreSQL CI database')
    marker = tmp_path / 'worker-entered'
    child_code = '''
import sys, time
from pathlib import Path
from db import engine, SessionLocal
from models import AppSetting
from portals.schedule import run_due
def unfinished():
    with SessionLocal() as db:
        db.add(AppSetting(key='synthetic.crash.uncommitted', value='must rollback'))
        db.flush()
        Path(sys.argv[1]).write_text('entered')
        time.sleep(60)
run_due(engine, 'synthetic-hard-kill', 86400, unfinished)
'''
    child = subprocess.Popen([sys.executable, '-c', child_code, str(marker)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), 'Child did not reach its uncommitted transaction'
        assert not run_due(engine, 'synthetic-hard-kill', 86400, lambda: None)
        child.kill()
        child.wait(timeout=5)
        # The server releases session locks when the killed connection closes.
        deadline = time.monotonic() + 5
        calls = []
        recovered = False
        while time.monotonic() < deadline:
            recovered = run_due(engine, 'synthetic-hard-kill', 86400, lambda: calls.append(1))
            if recovered:
                break
            time.sleep(0.05)
        assert recovered and calls == [1]
        assert db_session.get(AppSetting, 'synthetic.crash.uncommitted') is None
        assert not run_due(engine, 'synthetic-hard-kill', 86400, lambda: calls.append(2))
        assert calls == [1]
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_partial_discovery_without_pending_details_resumes_before_tomorrow(db_session):
    engine=db_session.get_bind();now=datetime(2026,9,26,tzinfo=timezone.utc)
    partial=lambda:{'merged':{'pending':0,'discovery_pending':1}}
    assert run_due(engine,'partial-search',86400,partial,now=now)
    assert run_due(engine,'partial-search',86400,partial,now=now+timedelta(minutes=5))
    complete=lambda:{'merged':{'pending':0,'discovery_pending':0}}
    assert run_due(engine,'partial-search',86400,complete,now=now+timedelta(minutes=10))
    assert not run_due(engine,'partial-search',86400,complete,now=now+timedelta(minutes=15))


@pytest.mark.parametrize('kind',['for_sale','sold'])
def test_storage_reports_incomplete_search_even_with_zero_unread_details(db_session,monkeypatch,kind):
    import json
    from config import settings
    from portals import direct, checkpoints
    from portals.storage import collect_and_stage
    monkeypatch.setattr(settings,'scraper_seeds',json.dumps({'homes':{kind:['https://homes.co.nz/map/auckland']}}))
    def partial(source,*,kind,cap,checkpoint,**kwargs):
        checkpoint['pending_urls']=[]
        checkpoint['discovery_coverage']={'loaded':133,'total':4932,'complete':False}
        return []
    monkeypatch.setattr(direct,'collect',partial)
    result=collect_and_stage(db_session,sources=['homes'],kind=kind,cap=10)
    assert result['merged']['pending']==0
    assert result['merged']['discovery_pending']==1
    assert checkpoints.load(db_session,'homes',kind)['discovery_coverage']['complete'] is False


def test_availability_job_has_separate_explicit_switch(monkeypatch):
    from config import settings
    from scraper_worker import jobs
    monkeypatch.setattr(settings,'scraper_check_listings',False)
    job=next(j for j in jobs() if j.name=='listing availability')
    assert not job.enabled()
    monkeypatch.setattr(settings,'scraper_check_listings',True)
    assert job.enabled() and job.every==1800


def test_failed_sources_do_not_mark_the_daily_job_successful(db_session):
    from portals.schedule import run_due
    from models import AppSetting
    import hashlib
    name='isolated-source-regression'
    key='scraper.schedule.'+hashlib.sha256(name.encode()).hexdigest()[:32]
    assert run_due(db_session.get_bind(),name,86400,lambda:{'merged':{'new':1,'failed_sources':1}})
    assert db_session.get(AppSetting,key) is None


@pytest.mark.parametrize('name',['new listings sweep','sold sweep'])
@pytest.mark.parametrize('previous,next_run',[
    ('2026-09-25T17:00:00+00:00','2026-09-26T16:00:00+00:00'),
    ('2027-04-02T16:00:00+00:00','2027-04-03T17:00:00+00:00'),
])
def test_collection_fixed_auckland_5am_across_dst(db_session,name,previous,next_run):
    engine=db_session.get_bind();calls=[]
    first=datetime.fromisoformat(previous);next_time=datetime.fromisoformat(next_run)
    assert run_due(engine,name,86400,lambda:calls.append(1),now=first)
    assert not run_due(engine,name,86400,lambda:calls.append(1),now=next_time-timedelta(seconds=1))
    assert run_due(engine,name,86400,lambda:calls.append(1),now=next_time)
    assert len(calls)==2


def test_late_collection_does_not_shift_next_5am(db_session):
    engine=db_session.get_bind();name='new listings sweep'
    late=datetime(2026,9,28,9,tzinfo=timezone.utc) # 22:00 Auckland
    assert run_due(engine,name,86400,lambda:None,now=late)
    assert run_due(engine,name,86400,lambda:None,now=datetime(2026,9,28,16,tzinfo=timezone.utc))
