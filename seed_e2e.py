#!/usr/bin/env python3
"""Build the database the browser tests run against.

A fixed, known world: one admin, one promoter with a referral, one paying
customer, one on trial, and a small batch of Auckland listings across two
districts. Every account uses the same password so a test never fails because
of a credential rather than the thing it is testing.

Destructive on purpose — it wipes and rebuilds, so a run always starts from the
same state and a test that passed yesterday cannot pass today because of a row
left behind by a different test.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/e2e.db")
os.environ.setdefault("JWT_SECRET", "e2e-secret")
os.environ.setdefault("CORS_ORIGINS", "*")
os.environ.setdefault("SEED_ADMIN_EMAIL", "admin@apexdemo.co.nz")
os.environ.setdefault("SEED_ADMIN_PASSWORD", "TestPass123")

from app import models as M                                    # noqa: E402
from app import promoters as P                                 # noqa: E402
from app.db import Base, SessionLocal, engine                  # noqa: E402
from app.security import hash_password                         # noqa: E402

PW = "TestPass123"
NOW = datetime.now(timezone.utc)

# Two districts, so the district -> suburb narrowing has something to narrow.
LISTINGS = [
    ("Remuera",    "Auckland City", 1_850_000, 2_100_000, 2_400_000, 220, 810, 4, 2),
    ("Remuera",    "Auckland City", 2_400_000, 2_600_000, 2_900_000, 280, 950, 5, 3),
    ("Mount Eden", "Auckland City", 1_450_000, 1_600_000, 1_800_000, 165, 620, 3, 1),
    ("Mount Eden", "Auckland City", 1_200_000, 1_350_000, 1_500_000, 140, 480, 3, 1),
    ("Papakura",   "Papakura",        820_000,   900_000, 1_050_000, 130, 700, 3, 1),
    ("Papakura",   "Papakura",        760_000,   840_000,   980_000, 120, 660, 3, 1),
    ("Takapuna",   "North Shore",   2_100_000, 2_300_000, 2_600_000, 210, 720, 4, 2),
]


def main() -> int:
    # Delete the FILE, do not try to empty the tables.
    #
    # Enumerating tables to clear looked tidy and broke on the second run: the
    # first run's browsing had written bug reports and settings rows pointing at
    # users, foreign keys are enforced, and the delete failed. Any list like that
    # is a list someone has to remember to update — and the failure mode is a
    # test suite that only works once.
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        path = url.replace("sqlite:////", "/").replace("sqlite:///", "")
        if path and os.path.exists(path):
            os.remove(path)
            print(f"removed {path}")
    else:
        print("refusing to seed a non-sqlite database", file=sys.stderr)
        return 2

    engine.dispose()
    Base.metadata.create_all(engine)
    db = SessionLocal()

    admin = M.User(email="admin@apexdemo.co.nz", password_hash=hash_password(PW),
                   full_name="Test Admin", role="admin", status="approved")
    # A paying customer: approved AND subscribed, so neither gate can be the
    # reason a test fails.
    customer = M.User(email="customer@apexdemo.co.nz", password_hash=hash_password(PW),
                      full_name="Paying Customer", role="user", status="approved",
                      subscription_status="active", stripe_customer_id="cus_e2e_paying",
                      email_verified_at=NOW, phone_verified_at=NOW)
    trialing = M.User(email="trial@apexdemo.co.nz", password_hash=hash_password(PW),
                      full_name="Trial User", role="user", status="pending",
                      subscription_status="trialing", stripe_customer_id="cus_e2e_trial",
                      email_verified_at=NOW, phone_verified_at=NOW,
                      trial_ends_at=NOW + timedelta(days=5))
    promoter_user = M.User(email="promoter@apexdemo.co.nz", password_hash=hash_password(PW),
                           full_name="Test Promoter", role="promoter", status="approved")
    db.add_all([admin, customer, trialing, promoter_user])
    db.commit()
    for u in (admin, customer, trialing, promoter_user):
        db.refresh(u)

    promoter = M.Promoter(user_id=promoter_user.id, code="E2ETEST",
                          display_name="Test Promoter", rate=20.0, active=True,
                          payout_email="promoter@apexdemo.co.nz")
    db.add(promoter); db.commit(); db.refresh(promoter)

    # One paying referral and one still on trial, so the dashboard has both a
    # number that earns and a number that deliberately does not.
    for user, paid in ((customer, True), (trialing, False)):
        ref = M.Referral(promoter_id=promoter.id, user_id=user.id,
                         code_used="E2ETEST", campaign="e2e-reel")
        db.add(ref); db.commit(); db.refresh(ref)
        if paid:
            ref.first_paid_at = NOW - timedelta(days=40)
            db.add(M.Commission(promoter_id=promoter.id, referral_id=ref.id,
                                period="2026-07", amount=20.0, source="invoice"))
            db.add(M.Commission(promoter_id=promoter.id, referral_id=ref.id,
                                period="2026-08", amount=20.0, source="invoice"))
            db.commit()

    db.add(M.ReferralClick(promoter_id=promoter.id, visitor="e2e-visitor-0001",
                           day=NOW.strftime("%Y-%m-%d"), campaign="e2e-reel"))
    db.commit()

    batch = M.ImportBatch(batch_type="for_sale", region="Auckland",
                          filename="e2e.csv", rows_total=len(LISTINGS),
                          is_active=True, uploaded_by_id=admin.id)
    db.add(batch); db.commit(); db.refresh(batch)

    for i, (suburb, district, ask, cv, fair, floor, land, beds, baths) in enumerate(LISTINGS):
        db.add(M.PropertyForSale(
            import_batch_id=batch.id, region="Auckland", suburb=suburb, district=district,
            address=f"{i + 1} Test Street, {suburb}", asking_price=ask, cv_numeric=cv,
            fair_value=fair, floor_area_m2=floor, land_area_m2=land,
            beds=beds, baths=baths, is_held=False,
            latitude=-36.87 - i * 0.004, longitude=174.77 + i * 0.004,
            property_type="House"))
    db.commit()

    counts = {
        "users": db.query(M.User).count(),
        "listings": db.query(M.PropertyForSale).count(),
        "referrals": db.query(M.Referral).count(),
        "commissions": db.query(M.Commission).count(),
    }
    db.close()
    print(f"seeded: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
