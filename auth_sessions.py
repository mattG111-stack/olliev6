"""Server-issued session receipts survive a deployment's signing configuration.

Only a SHA-256 digest of the bearer token is stored. A signature mismatch is
never sufficient to authenticate: the exact token must have a live receipt,
and normal account/password-change checks still apply. Legacy JWTs keep their
existing signature verification; an unregistered token is never recovered.
"""
import hashlib
from datetime import datetime, timezone
from models import AuthSession


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def remember(db, token, user_id, issued_at, expires_at):
    db.add(AuthSession(token_digest=digest(token), user_id=user_id,
                       issued_at=issued_at, expires_at=expires_at))
    # The caller commits before returning the token; a failed write must never
    # hand the browser a session whose durability was only assumed.
    db.flush()


def receipt(db, token):
    row = db.get(AuthSession, digest(token))
    if row is None:
        return None
    expires = row.expires_at
    expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
    if row.revoked_at is not None or expires <= datetime.now(timezone.utc):
        return None
    issued = row.issued_at
    issued = issued.replace(tzinfo=timezone.utc) if issued.tzinfo is None else issued
    return {'sub': str(row.user_id), 'iat': issued.timestamp(), 'exp': expires.timestamp()}
