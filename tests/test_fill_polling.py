import threading
from portals import fill_worker
import worker
from config import settings


def test_poller_finds_later_requests_without_scheduler_tick(monkeypatch):
    monkeypatch.setattr(worker, '_stop', False)
    monkeypatch.setattr(settings, 'scraper_enabled', True)
    calls=[]
    def pending():
        calls.append('poll')
        if len(calls)==1: raise RuntimeError('temporary failure')
    def sleep(seconds):
        assert seconds==10
        if len(calls)==3: worker._stop=True
    monkeypatch.setattr(fill_worker, 'run_pending', pending)
    fill_worker.poll_pending(sleep=sleep)
    assert len(calls)==3


def test_dispatch_singleton_and_polling_independent_of_main_loop(monkeypatch):
    entered=threading.Event();release=threading.Event();calls=[]
    def poll():
        calls.append(1);entered.set();release.wait(2)
    monkeypatch.setattr(fill_worker, '_thread', None)
    monkeypatch.setattr(fill_worker, 'poll_pending', poll)
    try:
        fill_worker.dispatch()
        assert entered.wait(1)
        fill_worker.dispatch()
        assert calls==[1]
    finally:
        release.set();fill_worker._thread.join(2)


def test_disabled_poller_does_not_consume_jobs(monkeypatch):
    monkeypatch.setattr(worker, '_stop', False)
    monkeypatch.setattr(settings, 'scraper_enabled', False)
    monkeypatch.setattr(fill_worker, 'run_pending', lambda: (_ for _ in ()).throw(AssertionError()))
    fill_worker.poll_pending(sleep=lambda _: setattr(worker, '_stop', True))
