from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from db import get_db
from models import User, ImportBatch, PropertyForSale
from interest_memory import InterestMemorySetting, PropertyInterest, memory_summary, assistant_interest_brief
from routers.activity import router
from security import create_access_token


def setup(db):
    users = [User(email=f'memory-{i}@example.test', password_hash='unused', role='admin', status='approved') for i in range(2)]
    batch = ImportBatch(batch_type='for_sale', filename='synthetic', is_active=True, status='published', region='Auckland')
    db.add_all(users + [batch]); db.flush()
    homes = [PropertyForSale(import_batch_id=batch.id, address=f'{i} Test Road', suburb='Henderson',
        property_type='House', floor_area_m2=100, asking_price=700000, is_held=(i==3)) for i in range(1,4)]
    db.add_all(homes); db.commit()
    app = FastAPI(); app.include_router(router); app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    headers = [{'Authorization': 'Bearer ' + create_access_token(u.id)[0]} for u in users]
    return client, headers, homes, users


def test_opt_in_visibility_dedup_user_isolation_and_clear(db_session):
    c, h, p, users = setup(db_session)
    assert c.get('/api/activity/interests').status_code == 401
    assert c.get('/api/activity/interests',headers=h[0]).json()['enabled'] is False
    c.post('/api/activity/property',headers=h[0],json={'property_id':p[0].id})
    assert db_session.query(PropertyInterest).count() == 0
    for header in h:
        assert c.put('/api/activity/interests',headers=header,json={'enabled':True}).status_code == 200
    for _ in range(2):
        assert c.post('/api/activity/property',headers=h[0],json={'property_id':p[0].id}).status_code == 204
    assert c.get('/api/activity/interests',headers=h[0]).json()['property_count'] == 1
    assert c.get('/api/activity/interests',headers=h[0]).json()['areas'] == []
    assert c.post('/api/activity/property',headers=h[0],json={'property_id':p[2].id}).status_code == 404
    c.post('/api/activity/property',headers=h[0],json={'property_id':p[1].id})
    c.post('/api/activity/property',headers=h[1],json={'property_id':p[0].id})
    assert c.get('/api/activity/interests',headers=h[0]).json()['areas'] == [{'suburb':'Henderson','properties':2}]
    assert c.delete('/api/activity/interests',headers=h[0]).status_code == 204
    assert c.get('/api/activity/interests',headers=h[0]).json()['property_count'] == 0
    assert c.get('/api/activity/interests',headers=h[1]).json()['property_count'] == 1
    c.put('/api/activity/interests',headers=h[1],json={'enabled':False})
    assert db_session.query(PropertyInterest).count() == 0


def test_expired_signals_ignored_and_brief_requires_confirmation(db_session, monkeypatch):
    import interest_memory
    c,h,p,users = setup(db_session)
    db_session.add(InterestMemorySetting(user_id=users[0].id,enabled=True))
    now = datetime.now(timezone.utc)
    for i in range(3):
        db_session.add(PropertyInterest(user_id=users[0].id,property_id=100+i,suburb='Henderson',last_viewed=now-timedelta(days=31 if i==2 else 1)))
    db_session.commit()
    assert memory_summary(db_session,users[0].id)['property_count'] == 2
    # Session context normally owns a new read-only session; use fixture connection here.
    from contextlib import nullcontext
    monkeypatch.setattr(interest_memory,'SessionLocal',lambda: nullcontext(db_session))
    brief = assistant_interest_brief(users[0])
    assert 'Henderson' in brief and 'ask before applying' in brief
    assert 'Never override stated areas, budgets or must-haves' in brief
    assert assistant_interest_brief(users[1]) == ''
    assert assistant_interest_brief(SimpleNamespace()) == ''


def test_history_is_bounded_and_stale_rows_pruned_on_next_view(db_session):
    c,h,p,users = setup(db_session)
    c.put('/api/activity/interests',headers=h[0],json={'enabled':True})
    now = datetime.now(timezone.utc)
    db_session.add_all([PropertyInterest(user_id=users[0].id,property_id=1000+i,suburb='Other',last_viewed=now-timedelta(days=1,seconds=i)) for i in range(105)])
    db_session.add(PropertyInterest(user_id=users[0].id,property_id=9999,suburb='Old',last_viewed=now-timedelta(days=31)))
    db_session.commit()
    assert c.post('/api/activity/property',headers=h[0],json={'property_id':p[0].id}).status_code == 204
    assert db_session.query(PropertyInterest).filter_by(user_id=users[0].id).count() == 100
    assert db_session.get(PropertyInterest,(users[0].id,9999)) is None
    assert db_session.get(PropertyInterest,(users[0].id,p[0].id)) is not None
