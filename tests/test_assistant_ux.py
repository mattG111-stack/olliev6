import json
from unittest.mock import patch

import pytest

from assistant.tools import _pricing_comparison, get_property
from assistant.providers import Result
from models import AssistantLog, ImportBatch, PropertyForSale, User
from routers import assistant as routes


def test_discount_and_uplift_have_different_denominators():
    result = _pricing_comparison(749000, 881000)
    assert result['value_uplift_over_asking_pct'] == 17.62
    assert result['asking_discount_below_value_pct'] == 14.98
    assert result['gap_dollars'] == 132000


@pytest.mark.parametrize('ask,value', [(0, 100), (100, 0), (None, 100), (100, None), (float('nan'), 100), (100, float('inf'))])
def test_missing_or_invalid_prices_never_produce_a_discount(ask, value):
    result = _pricing_comparison(ask, value)
    assert result['asking_discount_below_value_pct'] is None
    json.dumps(result, allow_nan=False)


def test_above_value_asking_is_a_negative_discount():
    assert _pricing_comparison(120, 100)['asking_discount_below_value_pct'] == -20


def test_property_tool_returns_link_prices_and_cost_assumptions(db_session):
    batch = ImportBatch(batch_type='for_sale', filename='test.csv', is_active=True)
    db_session.add(batch); db_session.flush()
    p = PropertyForSale(import_batch_id=batch.id, floor_area_m2=100, address='Test Road',
                        asking_price=749000, fair_value=881000, best_net_gain=850000)
    db_session.add(p); db_session.commit()
    data = json.loads(get_property(p.id))
    assert data['apex_url'] == f'/property/{p.id}'
    assert data['pricing_comparison']['asking_discount_below_value_pct'] == 14.98
    assert 'services and consent' in data['subdivision_assumptions']['cost_allowances']
    assert 'consent' in data['subdivision_assumptions']['not_verified']


def test_fresh_chat_does_not_inherit_any_prior_log(db_session, monkeypatch):
    user = User(email='ux@example.test', password_hash='test', role='admin', status='approved')
    db_session.add(user); db_session.commit()
    db_session.add(AssistantLog(user_id=user.id, question='Old brief', answer='Old response', ok=True))
    db_session.commit()
    monkeypatch.setattr(routes.settings_store, 'quota_for', lambda *_: {'shared': False})
    monkeypatch.setattr(routes.settings_store, 'shared_key', lambda *_: (None, None))
    monkeypatch.setattr(routes.settings_store, 'workspace_id', lambda *_: None)
    seen = []
    def answer(me, question, turns, **kwargs):
        seen.extend(turns)
        return Result(text='New answer', tools_used=[], iterations=1, queries=[])
    monkeypatch.setattr(routes, 'ask', answer)
    with patch.object(routes, '_recent_memory', side_effect=AssertionError('old memory used')), \
         patch.object(routes, '_similar_answered', side_effect=AssertionError('other chats used')):
        routes.ask_question(routes.AskIn(question='Fresh question', history=[]), user, db_session)
    assert seen == []


def test_background_job_uses_only_current_history(db_session, monkeypatch):
    user = User(email='job@example.test', password_hash='test', role='admin', status='approved')
    db_session.add(user); db_session.commit()
    monkeypatch.setattr(routes.settings_store, 'shared_key', lambda *_: (None, None))
    monkeypatch.setattr(routes.settings_store, 'workspace_id', lambda *_: None)
    seen = []
    def answer(me, question, turns, **kwargs):
        seen.append([(t.role, t.content) for t in turns])
        return Result(text='Answer', tools_used=[], iterations=1, queries=[])
    monkeypatch.setattr(routes, 'ask', answer)
    # These would contaminate both fresh and follow-up conversations if used.
    monkeypatch.setattr(routes, '_recent_memory', lambda *_: [routes.Turn(role='assistant', content='OLD')])
    monkeypatch.setattr(routes, '_similar_answered', lambda *a, **k: [routes.Turn(role='assistant', content='OTHER USER')])
    for history in [[], [{'role': 'user', 'content': 'My budget is 800k'}]]:
        row = AssistantLog(user_id=user.id, question='Current question', status='running',
                           queries=json.dumps(history))
        db_session.add(row); db_session.commit()
        routes._run_ask_job(row.id, user.id)
        db_session.expire_all()
        assert db_session.get(AssistantLog, row.id).status == 'done'
    assert seen == [[], [('user', 'My budget is 800k')]]


def test_search_distinguishes_result_limit_from_total_matches(db_session):
    from assistant.tools import search_listings
    batch = ImportBatch(batch_type='for_sale', filename='limit.csv', is_active=True)
    db_session.add(batch); db_session.flush()
    for i in range(3):
        db_session.add(PropertyForSale(import_batch_id=batch.id, address=f'{i} Test Road',
                                       asking_price=700000, fair_value=800000, floor_area_m2=100))
    db_session.commit()
    data = json.loads(search_listings(limit=2))
    assert data['returned_count'] == 2
    assert data['total_matches'] == 3
    assert len(data['listings']) == 2
