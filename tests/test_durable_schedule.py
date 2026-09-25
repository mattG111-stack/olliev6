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
    assert {job.name for job in jobs()}=={'new listings sweep','sold sweep','daily validated pricing'}


def test_unfinished_saved_pass_resumes_without_waiting_a_day(db_session):
    engine=db_session.get_bind();calls=[];now=datetime(2026,9,26,tzinfo=timezone.utc)
    def partial():calls.append(1);return {'merged':{'pending':2}}
    assert run_due(engine,'partial',86400,partial,now=now)
    assert run_due(engine,'partial',86400,partial,now=now+timedelta(minutes=5))
    assert len(calls)==2
