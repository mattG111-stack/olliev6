"""Every question is kept, the admin can read them, and Ollie learns from them.

Three things the log has to support, and only the first was true:

  * it is written  — every question and answer, whoever asked
  * it is readable — by an ADMIN, and only an admin: what a customer asks says
                     what they are looking at and what they can spend
  * it is used     — the assistant saw only the asker's own last three
                     exchanges, so every account started from nothing and the
                     same question was worked out from scratch each time

The corpus is the point of keeping it.
"""
from __future__ import annotations

import pytest

from app.models import AssistantLog, User
from app.routers.assistant import _keywords, _similar_answered, all_questions
from app.security import hash_password


def _user(db, email, name="Someone", role="user"):
    u = User(email=email, password_hash=hash_password("x"), full_name=name,
             role=role, status="approved")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _ask(db, user, question, answer, ok=True, tools=None):
    import json as _json
    db.add(AssistantLog(user_id=user.id if user else None, question=question,
                        answer=answer, ok=ok,
                        tools_used=_json.dumps(tools) if tools else None))
    db.commit()


# ── who asked what ───────────────────────────────────────────────────────────

def test_the_admin_sees_every_question_with_who_asked_it(db_session):
    db = db_session
    a = _user(db, "amy@apexdemo.co.nz", "Amy Buyer")
    b = _user(db, "bob@apexdemo.co.nz", "Bob Investor")
    _ask(db, a, "What is happening in Remuera?", "Remuera is doing this.")
    _ask(db, b, "Most underpriced in Browns Bay?", "These three.", tools=["query_data"])

    rows = all_questions(limit=100, search=None, failures_only=False, _=a, db=db)
    assert [r.email for r in rows] == ["bob@apexdemo.co.nz", "amy@apexdemo.co.nz"], (
        "newest first, with the asker on each row"
    )
    assert rows[0].name == "Bob Investor"
    assert rows[0].tools == ["query_data"]


def test_the_admin_can_search_the_questions(db_session):
    db = db_session
    a = _user(db, "amy@apexdemo.co.nz")
    _ask(db, a, "What is happening in Remuera?", "…")
    _ask(db, a, "How is Glenfield selling?", "…")
    found = all_questions(limit=100, search="glenfield", failures_only=False, _=a, db=db)
    assert len(found) == 1 and "Glenfield" in found[0].question


def test_failures_can_be_read_on_their_own(db_session):
    db = db_session
    a = _user(db, "amy@apexdemo.co.nz")
    _ask(db, a, "one that worked", "an answer")
    _ask(db, a, "one that broke", "TypeError: nope", ok=False)
    bad = all_questions(limit=100, search=None, failures_only=True, _=a, db=db)
    assert len(bad) == 1 and bad[0].question == "one that broke"
    assert bad[0].ok is False


# ── learning from all of them ────────────────────────────────────────────────

def test_a_question_draws_on_what_others_have_already_asked(db_session):
    """The whole point: a new account is not starting from nothing."""
    db = db_session
    veteran = _user(db, "vet@apexdemo.co.nz")
    newcomer = _user(db, "new@apexdemo.co.nz")
    _ask(db, veteran, "Which Remuera houses are underpriced?",
         "Remuera: 12 Test Rd at 14% under.")
    _ask(db, veteran, "How is the Hamilton rental market?", "Not covered.")

    turns = _similar_answered(db, "underpriced houses in Remuera",
                              exclude_user=newcomer.id)
    assert turns, "nothing was drawn from the corpus"
    assert "Remuera" in turns[0].content
    assert turns[1].role == "assistant" and "12 Test Rd" in turns[1].content
    # The unrelated question must not come along for the ride.
    assert all("Hamilton" not in t.content for t in turns)


def test_your_own_history_is_not_served_back_twice(db_session):
    """It is already fed in separately; duplicating it wastes the context."""
    db = db_session
    me = _user(db, "me@apexdemo.co.nz")
    _ask(db, me, "Which Remuera houses are underpriced?", "These ones.")
    assert _similar_answered(db, "underpriced Remuera houses", exclude_user=me.id) == []


def test_a_question_nobody_has_asked_draws_nothing(db_session):
    db = db_session
    a = _user(db, "a@apexdemo.co.nz")
    _ask(db, a, "Which Remuera houses are underpriced?", "These ones.")
    assert _similar_answered(db, "what is the weather in Dunedin",
                             exclude_user=None) == []


def test_a_failed_question_is_never_offered_as_an_example(db_session):
    """Learning from an answer that was an exception is worse than nothing."""
    db = db_session
    a = _user(db, "a@apexdemo.co.nz")
    _ask(db, a, "Which Remuera houses are underpriced?",
         "TypeError: nope", ok=False)
    assert _similar_answered(db, "underpriced houses in Remuera",
                             exclude_user=None) == []


@pytest.mark.parametrize("q,expected_absent", [
    ("What is the most underpriced house in Remuera?", {"what", "is", "the", "in", "most"}),
    ("Show me sales in Glenfield", {"show", "me", "in"}),
])
def test_common_words_are_not_what_makes_two_questions_alike(q, expected_absent):
    """Without this every question matches every other one on 'what' and 'the'."""
    words = _keywords(q)
    assert not (words & expected_absent), words
    assert any(w in words for w in ("remuera", "glenfield")), words
