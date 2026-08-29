"""The reason a question failed has to be readable by someone.

Every failure has been written to assistant_logs with its exception attached
since the catch-all went in — and there was nowhere to read it. So every report
carried the one fact that cannot be acted on, "500 from /api/assistant: HTTP
500", while the actual cause sat in a table nobody could see.
"""
from __future__ import annotations

from app.models import AssistantLog, User
from app.routers.assistant import recent_failures
from app.security import hash_password


def _user(db, email="asker@apexdemo.co.nz"):
    u = User(email=email, password_hash=hash_password("x"), full_name="Asker",
             role="user", status="approved")
    db.add(u); db.commit(); db.refresh(u)
    return u


def test_a_failed_question_is_listed_with_its_error(db_session):
    db = db_session
    u = _user(db)
    db.add(AssistantLog(user_id=u.id, question="What is happening in Remuera?",
                        answer="TypeError: unsupported operand type(s)", ok=False))
    db.add(AssistantLog(user_id=u.id, question="A question that worked",
                        answer="Here is the answer.", ok=True))
    db.commit()

    rows = recent_failures(limit=20, _=u, db=db)
    assert len(rows) == 1, "an answered question was listed as a failure"
    assert rows[0].question.startswith("What is happening")
    assert "TypeError" in (rows[0].error or ""), rows[0].error
    assert rows[0].email == "asker@apexdemo.co.nz"


def test_the_newest_failure_comes_first(db_session):
    db = db_session
    u = _user(db)
    for i in range(3):
        db.add(AssistantLog(user_id=u.id, question=f"q{i}", answer=f"Error {i}", ok=False))
    db.commit()
    rows = recent_failures(limit=20, _=u, db=db)
    assert [r.question for r in rows] == ["q2", "q1", "q0"]


def test_a_failure_from_a_deleted_account_still_shows(db_session):
    """The question and the error are the point; the account may be long gone."""
    db = db_session
    db.add(AssistantLog(user_id=None, question="orphan", answer="KeyError: 'x'", ok=False))
    db.commit()
    rows = recent_failures(limit=20, _=None, db=db)
    assert len(rows) == 1 and rows[0].email is None
    assert "KeyError" in (rows[0].error or "")
