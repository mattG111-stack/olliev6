from models import AssistantLog, User
from assistant.scope import RENTAL_REPLY
from settings_store import used_today

def test_rental_refusal_does_not_consume_allowance(db_session):
    u = User(email='scope-quota@test.example', password_hash='test', status='approved')
    db_session.add(u); db_session.flush()
    db_session.add_all([
        AssistantLog(user_id=u.id, question='Rent?', answer=RENTAL_REPLY, ok=True, status='done'),
        AssistantLog(user_id=u.id, question='Sales?', answer='A real sales answer', ok=True, status='done'),
        AssistantLog(user_id=u.id, question='Pending', answer=None, ok=True, status='running'),
        AssistantLog(user_id=u.id, question='Failure', answer='Error', ok=False, status='failed'),
    ])
    db_session.commit()
    assert used_today(db_session, u.id) == 1
