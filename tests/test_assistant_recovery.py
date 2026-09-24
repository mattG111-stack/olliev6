import json
import pytest
from assistant.tools import _money, get_property
from models import AssistantLog, User, ImportBatch, PropertyForSale
from routers.assistant import recent_asks

@pytest.mark.parametrize('value,expected', [(2964963, '$2,964,963'), (629999, '$629,999'), (-54321, '$-54,321'), (725.4, '$725'), (None, None), (float('nan'), None), (float('inf'), None)])
def test_tool_money_preserves_recorded_dollars(value, expected):
    assert _money(value) == expected

def test_property_price_and_comparison_use_same_record(db_session):
    batch = ImportBatch(batch_type='for_sale', filename='precision.csv', is_active=True)
    db_session.add(batch); db_session.flush()
    p = PropertyForSale(import_batch_id=batch.id, address='Precision Road', asking_price=2100000, fair_value=2964963)
    db_session.add(p); db_session.commit()
    data = json.loads(get_property(p.id))
    assert data['our_value'] == '$2,964,963'
    assert data['pricing_comparison']['gap_dollars'] == 864963

def test_recent_recovery_is_owned_bounded_and_read_only(db_session):
    owner = User(email='owner@recovery.test', password_hash='test', role='admin', status='approved')
    other = User(email='other@recovery.test', password_hash='test', status='approved')
    db_session.add_all([owner, other]); db_session.flush()
    rows = [AssistantLog(user_id=owner.id, question=f'Question {n}', status='done', answer='Saved answer') for n in range(12)]
    db_session.add_all(rows)
    db_session.add(AssistantLog(user_id=other.id, question='Private other question', status='running'))
    db_session.commit()
    count = db_session.query(AssistantLog).count()
    result = recent_asks(owner, db_session)
    assert len(result) == 10
    assert [r.ask_id for r in result] == [r.id for r in reversed(rows[-10:])]
    assert all(r.question != 'Private other question' for r in result)
    assert db_session.query(AssistantLog).count() == count
