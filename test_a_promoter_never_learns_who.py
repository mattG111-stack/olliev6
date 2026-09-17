"""A promoter is told how many, never who.

Handing an influencer a list of the platform's customers is a privacy incident
with a business reason attached, which does not make it less of one.

The promoter dashboard is the one place in the app where a person who is not an
admin is shown data derived from OTHER people's accounts. It reports a count, a
state and an amount earned per referral — deliberately no email, no name, no
phone. That is correct today; the risk is that it stays correct, because the
obvious next feature ("let promoters see who signed up so they can follow up")
is one field away and reads like a reasonable request.

So this is asserted twice over: on the shape of the payload, and on the actual
bytes of a real response with a real referred customer behind it. The second
matters more — a field added to a nested model, or a stray dict passed through,
would slip past a check on the declared schema alone.
"""
from __future__ import annotations

import json

import pytest

from app.models import User, UserRole, UserStatus

CUSTOMER_EMAIL = "private.customer@example.com"
CUSTOMER_NAME = "Aroha Wiremu"
CUSTOMER_PHONE = "0211234567"

# Anything that names a person, however it is spelled.
IDENTIFYING = ("email", "full_name", "name", "phone", "address")


def test_the_referral_payload_declares_no_way_to_identify_anyone():
    """The schema, first. Cheap, and it names the fields so a reviewer adding
    one has to argue with this test rather than with nobody."""
    from app.routers.promoters import ReferralOut

    fields = set(ReferralOut.model_fields)
    leaked = {f for f in fields if any(w in f.lower() for w in IDENTIFYING)}
    assert not leaked, f"the promoter payload now carries {sorted(leaked)}"
    assert fields == {"id", "joined", "state", "months_paid", "earned"}, (
        f"the referral payload has changed shape: {sorted(fields)} — if that is "
        f"deliberate, check the new field cannot identify a customer")


def test_a_real_dashboard_response_contains_no_customer_identity(promoter_client,
                                                                 referred_customer):
    """The bytes, second. A field added to a nested model, or a dict passed
    straight through, would slip past a check on the declared schema."""
    r = promoter_client.get("/api/promoter/dashboard")
    assert r.status_code == 200, r.text
    body = json.dumps(r.json()).lower()

    for secret in (CUSTOMER_EMAIL, CUSTOMER_NAME, CUSTOMER_PHONE):
        assert secret.lower() not in body, \
            f"the promoter dashboard leaked {secret!r}"
    # And the local-part on its own, in case the domain is stripped somewhere.
    assert "private.customer" not in body
    assert "aroha" not in body


def test_the_promoter_still_learns_what_they_are_owed(promoter_client,
                                                      referred_customer):
    """The counterweight. A dashboard that reports nothing is private and
    useless; the promoter has to be able to see that the referral happened and
    what it is worth, or they cannot check their own pay."""
    body = promoter_client.get("/api/promoter/dashboard").json()

    assert body["referrals"], "the referral vanished along with the identity"
    row = body["referrals"][0]
    assert set(row) == {"id", "joined", "state", "months_paid", "earned"}
    assert row["state"], "a referral with no state tells the promoter nothing"


def test_a_promoter_cannot_reach_the_admin_promoter_list(promoter_client):
    """That list DOES carry emails — the promoters' own, which an admin needs
    for payouts. A promoter reaching it would read every other promoter's
    contact details."""
    r = promoter_client.get("/api/admin/promoters")
    assert r.status_code in (401, 403), \
        f"a promoter reached the admin promoter list ({r.status_code})"


@pytest.fixture()
def referred_customer(db_session, promoter_row):
    """A paying customer who signed up through the promoter's link.

    Attribution lives in its own Referral row rather than on the user — one
    customer, one referrer, enforced by a unique constraint — so the fixture
    has to build the same link the sign-up does.
    """
    from app.models import Referral

    u = User(email=CUSTOMER_EMAIL, password_hash="x", full_name=CUSTOMER_NAME,
             phone=CUSTOMER_PHONE, role=UserRole.USER.value,
             status=UserStatus.APPROVED.value, subscription_status="active")
    db_session.add(u)
    db_session.flush()
    db_session.add(Referral(promoter_id=promoter_row.id, user_id=u.id,
                           code_used=promoter_row.code))
    db_session.commit()
    return u


@pytest.fixture()
def promoter_row(db_session, promoter_user):
    from app.models import Promoter

    p = Promoter(user_id=promoter_user.id, code="TESTCODE", rate=0.2, active=True)
    db_session.add(p)
    db_session.commit()
    return p


@pytest.fixture()
def promoter_user(db_session):
    u = User(email="promoter@test.local", password_hash="x",
             role=UserRole.PROMOTER.value, status=UserStatus.APPROVED.value)
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def promoter_client(db_session, promoter_user):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.security import current_user, require_active, require_promoter

    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: promoter_user,
        require_active: lambda: promoter_user,
        require_promoter: lambda: promoter_user,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}
