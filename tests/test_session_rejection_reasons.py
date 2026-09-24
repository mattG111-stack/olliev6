from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import logging
import jwt
import pytest
from fastapi import HTTPException
from config import settings
from security import current_user, create_access_token


class Database:
    def get(self, model, id):
        return SimpleNamespace(id=id,status='approved') if id==42 else None


def test_signed_token_survives_independent_validation_without_process_state(monkeypatch):
    monkeypatch.setattr(settings,'jwt_secret','synthetic-persistent-session-secret')
    token,_=create_access_token(42)
    assert current_user(token,Database()).id==42
    assert current_user(token,Database()).id==42


@pytest.mark.parametrize('case,reason',[('expired','token_expired'),('wrong_key','signature_mismatch'),('missing_user','user_missing'),('malformed','invalid_token')])
def test_session_failure_diagnostics_never_log_credentials(monkeypatch,caplog,case,reason):
    monkeypatch.setattr(settings,'jwt_secret','synthetic-persistent-session-secret')
    payload={'sub':'99' if case=='missing_user' else '42','exp':datetime.now(timezone.utc)+timedelta(minutes=-1 if case=='expired' else 10)}
    token='malformed-test-token' if case=='malformed' else jwt.encode(payload,'other-test-secret' if case=='wrong_key' else settings.jwt_secret,algorithm=settings.jwt_algorithm)
    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as err:
        current_user(token,Database())
    assert err.value.status_code==401
    assert reason in caplog.text
    assert token not in caplog.text and settings.jwt_secret not in caplog.text
    assert err.value.detail=='Could not validate credentials'
