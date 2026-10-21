from datetime import datetime, timedelta, timezone
import logging
import jwt
import pytest
from fastapi import HTTPException
from config import settings
from models import AuthSession, User
from security import create_access_token, current_user


def issued(db):
    u = User(email='session@example.test', password_hash='synthetic', status='approved', role='admin')
    db.add(u); db.commit()
    token, _ = create_access_token(u.id, db)
    db.commit()
    return u, token


def test_exact_issued_session_survives_signer_change_and_new_db_session(db_session, monkeypatch, caplog):
    monkeypatch.setattr(settings, 'jwt_secret', 'synthetic-first-signing-configuration')
    u, token = issued(db_session)
    row = db_session.query(AuthSession).one()
    assert len(row.token_digest) == 64 and token not in row.token_digest
    assert current_user(token, db_session).id == u.id
    monkeypatch.setattr(settings, 'jwt_secret', 'synthetic-second-signing-configuration')
    from db import SessionLocal
    with caplog.at_level(logging.INFO), SessionLocal() as fresh:
        assert current_user(token, fresh).id == u.id
    assert token not in caplog.text and settings.jwt_secret not in caplog.text


def test_unregistered_or_changed_token_never_recovers(db_session, monkeypatch):
    monkeypatch.setattr(settings, 'jwt_secret', 'synthetic-first-signing-configuration')
    u, token = issued(db_session)
    unregistered, _ = create_access_token(u.id)
    forged = jwt.encode({'sub':str(u.id), 'sid':'made-up', 'exp':datetime.now(timezone.utc)+timedelta(hours=1)},
                        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    # Even a valid signature cannot invent a receipt for a new-format session.
    with pytest.raises(HTTPException): current_user(forged, db_session)
    monkeypatch.setattr(settings, 'jwt_secret', 'synthetic-second-signing-configuration')
    for bad in [unregistered, token[:-3]+'bad']:
        with pytest.raises(HTTPException): current_user(bad, db_session)


@pytest.mark.parametrize('reason', ['expired', 'revoked', 'password', 'deactivated'])
def test_recovery_preserves_expiry_revocation_and_account_checks(db_session, monkeypatch, reason):
    u, token = issued(db_session)
    record = db_session.query(AuthSession).one()
    if reason == 'expired': record.expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
    if reason == 'revoked': record.revoked_at = datetime.now(timezone.utc)
    if reason == 'password': u.password_changed_at = datetime.now(timezone.utc)
    if reason == 'deactivated': u.status = 'deactivated'
    db_session.commit()
    monkeypatch.setattr(settings, 'jwt_secret', 'synthetic-changed-signing-configuration')
    with pytest.raises(HTTPException) as err: current_user(token, db_session)
    assert err.value.status_code == (403 if reason == 'deactivated' else 401)


def test_revoked_receipt_is_rejected_even_without_configuration_change(db_session):
    _, token = issued(db_session)
    db_session.query(AuthSession).one().revoked_at = datetime.now(timezone.utc)
    db_session.commit()
    with pytest.raises(HTTPException): current_user(token, db_session)


def test_sign_in_persists_receipt_before_returning_token(db_session):
    from routers.auth import sign_in
    from fastapi.security import OAuth2PasswordRequestForm
    from security import hash_password
    u = User(email='login@example.test', password_hash=hash_password('Synthetic1'), status='approved')
    db_session.add(u); db_session.commit()
    result = sign_in(OAuth2PasswordRequestForm(username=u.email, password='Synthetic1'), db_session)
    assert db_session.query(AuthSession).count() == 1
    assert current_user(result.access_token, db_session).id == u.id


def test_assistant_cannot_query_session_receipts():
    from assistant.tools import query_data
    result = query_data('SELECT * FROM auth_sessions')
    assert result.startswith("Query rejected: Table 'auth_sessions' is not queryable")


def test_fresh_process_accepts_registered_session_after_signer_change(db_session):
    import os, subprocess, sys
    u, token = issued(db_session)
    env = dict(os.environ, JWT_SECRET='synthetic-other-process-signing-configuration')
    script = ('import sys; from db import SessionLocal; from security import current_user; '
              'db=SessionLocal(); assert current_user(sys.stdin.read(),db).id==' + str(u.id))
    result = subprocess.run([sys.executable, '-c', script], input=token, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
