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


def test_session_token_survives_fresh_python_processes_without_key_rotation():
    import os, subprocess, sys
    environment = dict(os.environ, JWT_SECRET='synthetic-continuity-key-at-least-32-characters',
                       DATABASE_URL='sqlite:////tmp/apex-session-continuity-synthetic.db')
    mint = "from security import create_access_token; print(create_access_token(42)[0])"
    token = subprocess.run([sys.executable,'-c',mint],env=environment,capture_output=True,text=True,check=True).stdout.strip()
    verify = "import sys,jwt; from config import settings; assert jwt.decode(sys.stdin.read(),settings.jwt_secret,algorithms=[settings.jwt_algorithm])['sub']=='42'"
    subprocess.run([sys.executable,'-c',verify],input=token,env=environment,capture_output=True,text=True,check=True)
