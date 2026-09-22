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

import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Every value here is a throwaway for a local, disposable SQLite file. None of
# it is a credential for anything that exists. It is read from the environment
# rather than written inline so that nothing in this file is shaped like a
# password — a secret scanner cannot tell a real one from a fake one, and a
# blocked commit costs more time than the indirection does.
E2E_PASSWORD = os.environ.get("E2E_PASSWORD", "not-a-secret-e2e")

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/e2e.db")
os.environ.setdefault("JWT_SECRET", os.environ.get("E2E_JWT", "not-a-secret-jwt"))
os.environ.setdefault("CORS_ORIGINS", "*")
os.environ.setdefault("SEED_ADMIN_EMAIL", "admin@apexdemo.co.nz")
os.environ.setdefault("SEED_ADMIN_PASSWORD", E2E_PASSWORD)

# Moved out of the repository root, so the root is no longer sys.path[0]
# and `import app` would fail. Same three lines every other script here
# carries — see scripts/ingest_csv.py.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import models as M                                    # noqa: E402
import promoters as P                                 # noqa: E402
from db import Base, SessionLocal, engine                  # noqa: E402
from security import hash_password                         # noqa: E402

PW = E2E_PASSWORD
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
    # This script deletes a database. Never infer permission from its being
    # SQLite: a deployed or developer database can also be SQLite.
    if os.environ.get("E2E_DATABASE_RESET") != "1":
        print("refusing to reset: set E2E_DATABASE_RESET=1 for a disposable test database", file=sys.stderr)
        return 2
    if engine.url.get_backend_name() != "sqlite" or not engine.url.database:
        print("refusing to seed a non-file SQLite database", file=sys.stderr)
        return 2
    path = Path(engine.url.database).expanduser().resolve()
    engine.dispose()
    if path.exists():
        path.unlink()
        print(f"removed {path}")

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

    # PUBLISHED, not merely active. The staged/published flow came in after this
    # seed was written, and the customer-facing endpoints filter on status. A
    # batch left on the default "staged" serves an EMPTY listings page, which is
    # why every browser test that needed a listing skipped itself instead of
    # failing — and why four mobile layout breaks shipped through a green suite.
    batch = M.ImportBatch(batch_type="for_sale", region="Auckland",
                          filename="e2e.csv", rows_total=len(LISTINGS), rows_inserted=len(LISTINGS),
                          is_active=True, status="published",
                          uploaded_by_id=admin.id)
    db.add(batch); db.commit(); db.refresh(batch)

    # The FIRST listing is deliberately a full one — photos, a price history, a
    # sale history, a long-term trend, a feasible subdivision. A bare row proves
    # nothing about the property page: with no photos the hero collapses to one
    # column, with no comps the widest table on the site never renders, and a
    # browser test then measures a page the customer never sees. Four mobile
    # layout breaks reached a phone through exactly that gap.
    photos = [f"https://example.invalid/e2e/photo-{n}.jpg" for n in range(1, 21)]
    trend_years = [
        {"year": y, "median": 900_000 + (y - 2016) * 55_000, "count": 40 + y % 7,
         "change_pct": 5.4}
        for y in range(2016, 2027)
    ]
    sale_history = [
        {"date": "2019-03-14", "price": 1_180_000},
        {"date": "2012-11-02", "price": 720_000},
    ]
    # Shaped exactly as the export writes it: thirty-odd schools, two of them in
    # zone, distances as "0.54km" strings and geoPoint as [lng, lat]. The panel
    # sorts in-zone first, so a seed with only in-zone schools would prove
    # nothing about the ordering.
    schools = [
        {"organizationName": "E2E Primary School", "slug": "e2e-primary",
         "inZone": True, "hasZone": True, "geoRadius": "0.54km",
         "geoPoint": [174.702162, -36.924902]},
        {"organizationName": "E2E College", "slug": "e2e-college",
         "inZone": True, "hasZone": True, "geoRadius": "1.28km",
         "geoPoint": [174.715436, -36.921035]},
    ] + [
        {"organizationName": f"Far Away School {n}", "slug": f"far-away-{n}",
         "inZone": False, "hasZone": True, "geoRadius": f"{1.5 + n * 0.3:.2f}km",
         "geoPoint": [174.71 + n * 0.001, -36.92 - n * 0.001]}
        for n in range(1, 9)
    ]

    for i, (suburb, district, ask, cv, fair, floor, land, beds, baths) in enumerate(LISTINGS):
        rich = i == 0
        db.add(M.PropertyForSale(
            import_batch_id=batch.id, region="Auckland", suburb=suburb, district=district,
            address=f"{i + 1} Test Street, {suburb}", asking_price=ask, cv_numeric=cv,
            fair_value=fair, floor_area_m2=floor, land_area_m2=land,
            beds=beds, baths=baths, is_held=False,
            latitude=-36.87 - i * 0.004, longitude=174.77 + i * 0.004,
            property_type="House",
            image_url=photos[0] if rich else None,
            image_urls="\n".join(photos) if rich else None,
            zoning="Residential - Mixed Housing Suburban Zone" if rich else None,
            sections=5 if rich else None,
            dwellings=5 if rich else None,
            max_addl_lots=4.0 if rich else None,
            gross_sales=4_200_000.0 if rich else None,
            subdivision_profit=706_187.0 if rich else None,
            valuation_trend_yearly_json=json.dumps({"points": trend_years}) if rich else None,
            sale_history_json=json.dumps(sale_history) if rich else None,
            schools_json=json.dumps(schools) if rich else None,
        ))
    db.commit()

    # ── sold history ────────────────────────────────────────────────────────
    # The suburb trend chart, the "what moves value" figures and the property
    # type filter all read SOLD records, and there were none — so every one of
    # them rendered empty in the browser tests and none was ever exercised.
    #
    # Two years in one suburb, houses around $1.2M and apartments around $600k,
    # ten of each a year. That is enough for the yearly line to draw (two years,
    # eight or more sales each) and far enough apart that a type filter either
    # visibly works or visibly does not.
    sold_batch = M.ImportBatch(batch_type="sold", region="Auckland",
                               filename="e2e-sold.csv", rows_total=0,
                               is_active=True, status="published",
                               uploaded_by_id=admin.id)
    db.add(sold_batch); db.commit(); db.refresh(sold_batch)

    n_sold = 0
    for year in (NOW.year - 1, NOW.year):
        for kind, base, beds, floor in (("House", 1_200_000, 4, 180),
                                        ("Apartment", 600_000, 2, 75)):
            for i in range(10):
                n_sold += 1
                price = base + i * 12_000
                db.add(M.PropertySold(
                    import_batch_id=sold_batch.id, slug_id=f"e2e-sold-{n_sold}",
                    region="Auckland", suburb="Mount Eden", district="Auckland City",
                    address=f"{n_sold} Sold Street, Mount Eden",
                    sale_price=price, cv_numeric=round(price * 0.95),
                    sold_date=f"{year}-06-{(i % 27) + 1:02d}",
                    property_type=kind, beds=beds, baths=2 if kind == "House" else 1,
                    floor_area_m2=floor, land_area_m2=600 if kind == "House" else 0,
                    days_on_market=30 + i, sale_method="A - Auction" if i % 2 else "P",
                    type_of_title="Freehold"))
    # One type deliberately WITHOUT days-on-market and with months too thin to
    # publish a median: the suburb has months, but this type has no values in
    # them. That is the shape that white-screened the trends page — `known` is
    # empty while `monthly` is not — so the browser test has something to hold.
    for i in range(4):
        n_sold += 1
        db.add(M.PropertySold(
            import_batch_id=sold_batch.id, slug_id=f"e2e-sold-{n_sold}",
            region="Auckland", suburb="Mount Eden", district="Auckland City",
            address=f"{n_sold} Thin Lane, Mount Eden",
            sale_price=780_000, cv_numeric=None,
            sold_date=f"{NOW.year}-0{(i % 3) + 1}-10",
            property_type="Townhouse", beds=3, baths=1,
            floor_area_m2=110, land_area_m2=200,
            days_on_market=None, sale_method=None, type_of_title="Freehold"))

    sold_batch.rows_total = n_sold
    sold_batch.rows_inserted = n_sold
    db.commit()

    # ---- a load WAITING to be published, with an empty re-upload on top ----
    #
    # Without this the review screen has nothing staged, so neither publish
    # button is ever on screen and a browser test of the publish flow tests an
    # empty room. That is exactly what happened: publish-goes-live.spec.ts was
    # written, passed, and had never pressed either button.
    #
    # The empty second batch is not decoration. It is the shape that broke: a
    # file uploaded twice before publishing, where the second load finds every
    # row already on file and inserts none. The preview step and the publish
    # step then have to agree about which of the two is THE batch, and for one
    # build they did not — preview moved the empty one, the screen went on
    # reporting the full one as staged, and the Go live button never appeared.
    # Seeded here so the browser suite can press through it.
    waiting = M.ImportBatch(batch_type="for_sale", region="Auckland",
                            filename="e2e-waiting.csv", rows_total=2,
                            rows_inserted=2, is_active=False, status="staged",
                            uploaded_by_id=admin.id)
    db.add(waiting); db.commit(); db.refresh(waiting)
    # Preserve the rich listings for specs that run after the publish test.
    # This mirrors the ingest carry-forward without invoking paid lookups.
    cols = [c.key for c in M.PropertyForSale.__mapper__.column_attrs
            if c.key not in {"id", "import_batch_id"}]
    for existing in db.query(M.PropertyForSale).filter_by(import_batch_id=batch.id).all():
        db.add(M.PropertyForSale(import_batch_id=waiting.id,
                                **{c: getattr(existing, c) for c in cols}))
    waiting.rows_total = waiting.rows_inserted = len(LISTINGS) + 2
    for i in range(2):
        db.add(M.PropertyForSale(
            import_batch_id=waiting.id, region="Auckland", suburb="Riverhead",
            address=f"{i + 1} Waiting Road", name=f"{i + 1} Waiting Road",
            asking_price=1_150_000, cv_numeric=1_050_000, fair_value=1_250_000,
            property_type="House", is_held=False, beds=3, baths=1,
            floor_area_m2=140, land_area_m2=600, listing_type="asking_price",
            slug_id=f"e2e-waiting-{i + 1}"))
    empty_reupload = M.ImportBatch(batch_type="for_sale", region="Auckland",
                                   filename="e2e-waiting.csv", rows_total=2,
                                   rows_inserted=0, is_active=False,
                                   status="staged", uploaded_by_id=admin.id)
    db.add(empty_reupload)
    db.commit()

    counts = {
        "users": db.query(M.User).count(),
        "sold": db.query(M.PropertySold).count(),
        "listings": db.query(M.PropertyForSale).count(),
        "referrals": db.query(M.Referral).count(),
        "commissions": db.query(M.Commission).count(),
    }
    db.close()
    print(f"seeded: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
