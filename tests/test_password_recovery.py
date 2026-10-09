import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import password_recovery as r
from models import User, VerificationCode

@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    VerificationCode.__table__.create(engine)
    with Session(engine) as session:
        session.add(User(email="reset@example.com", password_hash="original"))
        session.commit()
        yield session

def request(db):
    return r.request_reset(r.ResetRequest(email="reset@example.com"), db)

def confirm(db, code="123456", password="Abcdef1"):
    return r.confirm_reset(r.ResetConfirm(email="reset@example.com", code=code, password=password),db)

def seed(db, **kwargs):
    row=VerificationCode(user_id=1,channel=r.CHANNEL,code="123456",attempts=0,
        expires_at=datetime.now(timezone.utc)+timedelta(minutes=15))
    for key,value in kwargs.items(): setattr(row,key,value)
    db.add(row); db.commit(); return row

def test_missing_email_config_does_not_send_or_change(db):
    with patch.object(r.settings,"resend_api_key",""), patch.object(r,"send_email") as send:
        with pytest.raises(r.HTTPException) as error: request(db)
        assert error.value.status_code==503
        send.assert_not_called()
        assert db.get(User,1).password_hash=="original"

def test_request_and_throttle(db):
    with patch.object(r.settings,"resend_api_key","test"),patch.object(r,"send_email",return_value=True) as send:
        request(db);request(db)
        assert send.call_count==1
        assert db.query(VerificationCode).count()==1
        assert db.get(User,1).password_hash=="original"

def test_delivery_failure_does_not_save_code(db):
    with patch.object(r.settings,"resend_api_key","test"),patch.object(r,"send_email",return_value=False):
        request(db)
        assert db.query(VerificationCode).count()==0

def test_single_use_and_hash(db):
    seed(db)
    with patch.object(r,"hash_password",return_value="new-hash"):
        confirm(db)
        assert db.get(User,1).password_hash=="new-hash"
        with pytest.raises(r.HTTPException): confirm(db)

def test_expired(db):
    seed(db,expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
    with pytest.raises(r.HTTPException):confirm(db)
    assert db.get(User,1).password_hash=="original"

def test_attempt_limit(db):
    row=seed(db)
    for _ in range(6):
        with pytest.raises(r.HTTPException):confirm(db,code="000000")
    assert row.attempts==6
    with pytest.raises(r.HTTPException):confirm(db)
    assert db.get(User,1).password_hash=="original"

def test_policy_preserves_code(db):
    row=seed(db)
    with pytest.raises(r.HTTPException):confirm(db,password="abcdef1")
    assert row.consumed_at is None
    assert row.attempts==0

def test_unknown_account_generic(db):
    with patch.object(r.settings,"resend_api_key","test"):
        assert r.request_reset(r.ResetRequest(email="unknown@example.com"),db)=={"detail":r.MESSAGE}
