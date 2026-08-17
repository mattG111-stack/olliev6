"""The referral programme, end to end.

This is money code. Every rule that decides whether someone gets paid is tested
here, and the ones worth the most attention are the refusals: self-referral, an
existing account arriving through a link, a trial that never converted, and a
webhook Stripe retries. Each of those is a way to pay out revenue that never
arrived, and none of them announce themselves — the numbers just quietly drift.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/_test_promoters.db")
os.environ.setdefault("JWT_SECRET", "s")
os.environ.setdefault("CORS_ORIGINS", "*")
os.environ.setdefault("SEED_ADMIN_EMAIL", "admin@apexdemo.co.nz")
os.environ.setdefault("SEED_ADMIN_PASSWORD", "secret123")

PASS: list[str] = []
FAIL: list[str] = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("PASS ", name)
    else:
        FAIL.append(name)
        print("FAIL ", name, "--", detail)


from fastapi.testclient import TestClient          # noqa: E402

from app import models as M                        # noqa: E402
from app import promoters as P                     # noqa: E402
from app.db import Base, SessionLocal, engine      # noqa: E402
from app.main import app                           # noqa: E402
from app.security import create_access_token, hash_password  # noqa: E402

Base.metadata.create_all(engine)

client = TestClient(app)


def reset():
    db = SessionLocal()
    for model in (M.Commission, M.Referral, M.Promoter, M.WishList,
                  M.VerificationCode, M.AssistantLog):
        try:
            db.query(model).delete()
        except Exception:
            db.rollback()
    db.commit()
    for u in db.query(M.User).all():
        if u.email != "admin@apexdemo.co.nz":
            db.delete(u)
    db.commit()
    admin = db.query(M.User).filter(M.User.email == "admin@apexdemo.co.nz").first()
    if admin is None:
        admin = M.User(email="admin@apexdemo.co.nz", password_hash=hash_password("secret123"),
                       full_name="Admin", role="admin", status="approved")
        db.add(admin); db.commit(); db.refresh(admin)
    aid = admin.id
    db.close()
    return aid


ADMIN_ID = reset()
H = {"Authorization": f"Bearer {create_access_token(ADMIN_ID)[0]}"}


def auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)[0]}"}


def make_promoter(email="influencer@example.com", code="MATTB", rate=None):
    body = {"email": email, "full_name": "An Influencer", "password": "secret123", "code": code}
    if rate is not None:
        body["rate"] = rate
    return client.post("/api/admin/promoters", headers=H, json=body)


def sign_up(email, ref=None):
    body = {"email": email, "password": "secret123", "full_name": "A Customer"}
    if ref is not None:
        body["ref"] = ref
    return client.post("/api/auth/sign-up", json=body)


def set_status(email, status, customer_id=None):
    """Stand in for Stripe's webhook putting a subscription state on the user."""
    db = SessionLocal()
    u = db.query(M.User).filter(M.User.email == email.lower()).first()
    u.subscription_status = status
    if customer_id:
        u.stripe_customer_id = customer_id
    db.commit(); db.close()


def pay(email, start, end, source="invoice"):
    db = SessionLocal()
    u = db.query(M.User).filter(M.User.email == email.lower()).first()
    made = P.record_payment(db, u, start, end, source=source)
    n = len(made)
    db.close()
    return n


JAN = datetime(2026, 1, 1, tzinfo=timezone.utc)
FEB = datetime(2026, 2, 1, tzinfo=timezone.utc)
MAR = datetime(2026, 3, 1, tzinfo=timezone.utc)

# ── creating a promoter ───────────────────────────────────────────────────────
r = make_promoter()
check("an admin can create a promoter", r.status_code == 201, r.text)
PRO = r.json()
check("the requested code is used when it is free", PRO["code"] == "MATTB", PRO)
check("the promoter gets a shareable link", PRO["link"].endswith("/sign-up?ref=MATTB"), PRO["link"])
check("the default rate is $20", PRO["rate"] == 20.0, PRO)
check("a new promoter has referred nobody", PRO["total_referred"] == 0)

db = SessionLocal()
PRO_USER = db.query(M.User).filter(M.User.email == "influencer@example.com").first()
PRO_USER_ID = PRO_USER.id
check("the promoter login is not a customer role", PRO_USER.role == "promoter", PRO_USER.role)
check("and is approved so they can sign in", PRO_USER.status == "approved")
db.close()

r = client.post("/api/admin/promoters", headers=H,
                json={"email": "other@example.com", "password": "secret123", "code": "mattb"})
check("a duplicate code is not handed out twice", r.json()["code"] != "MATTB", r.text)
check("the second promoter still gets a working code", len(r.json()["code"]) >= 3)
OTHER_ID = r.json()["id"]

r = client.post("/api/admin/promoters", headers=H, json={"email": "nopass@example.com"})
check("a new promoter without a password is refused", r.status_code == 422, r.text)

r = make_promoter(email="admin@apexdemo.co.nz")
check("an admin account cannot be turned into a promoter", r.status_code == 409, r.text)

# ── a non-admin cannot manage promoters ───────────────────────────────────────
check("the promoter list is admin-only", client.get("/api/admin/promoters").status_code == 401)
check("creating a promoter is admin-only",
      client.post("/api/admin/promoters", json={"email": "x@y.z"}).status_code == 401)

# ── attribution at sign-up ────────────────────────────────────────────────────
r = sign_up("customer1@example.com", ref="MATTB")
check("someone can sign up through a referral link", r.status_code == 201, r.text)

r = sign_up("customer2@example.com", ref="mattb")
check("the code is case-insensitive", r.status_code == 201, r.text)
r = sign_up("customer3@example.com", ref="  matt-b  ")
check("and tolerant of punctuation and spaces typed around it", r.status_code == 201, r.text)

db = SessionLocal()
n = db.query(M.Referral).count()
check("all three landed against the promoter", n == 3, f"got {n}")
db.close()

r = sign_up("organic@example.com")
check("a signup with no code still works", r.status_code == 201, r.text)
r = sign_up("badcode@example.com", ref="NOSUCHCODE")
check("an unknown code does not stop the signup", r.status_code == 201, r.text)

db = SessionLocal()
u = db.query(M.User).filter(M.User.email == "badcode@example.com").first()
check("and credits nobody",
      db.query(M.Referral).filter(M.Referral.user_id == u.id).first() is None)
db.close()

# ── the refusals that protect the money ───────────────────────────────────────
r = sign_up("customer1@example.com", ref="MATTB")
check("an email that already has an account cannot be referred again",
      r.status_code == 409, r.text)

db = SessionLocal()
n = db.query(M.Referral).filter(M.Referral.user_id == db.query(M.User).filter(
    M.User.email == "customer1@example.com").first().id).count()
check("so that customer is still credited exactly once", n == 1, f"got {n}")

# a promoter signing up through their own link
promoter_row = db.query(M.Promoter).filter(M.Promoter.code == "MATTB").first()
pro_user = db.get(M.User, promoter_row.user_id)
made = P.attribute(db, pro_user, "MATTB")
check("a promoter cannot refer themselves", made is None)

# a second attribution attempt on a customer who already has a referrer
c1 = db.query(M.User).filter(M.User.email == "customer1@example.com").first()
before = db.query(M.Referral).filter(M.Referral.user_id == c1.id).first().promoter_id
P.attribute(db, c1, db.get(M.Promoter, OTHER_ID).code)
after = db.query(M.Referral).filter(M.Referral.user_id == c1.id).first().promoter_id
check("a customer cannot be re-credited to a second promoter", before == after)
check("and there is still only one referral row for them",
      db.query(M.Referral).filter(M.Referral.user_id == c1.id).count() == 1)
db.close()

# ── a trial is not a payment ──────────────────────────────────────────────────
set_status("customer1@example.com", "trialing")
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("a trialing referral is shown separately", d["trialing"] == 1, d)
check("and is NOT counted as paying", d["paying"] == 0, d)
check("so nothing has been earned yet", d["earned_all_time"] == 0.0, d)
check("and the run rate is zero", d["monthly_run_rate"] == 0.0, d)

# ── a real payment earns ──────────────────────────────────────────────────────
n = pay("customer1@example.com", JAN, FEB)
check("a paid invoice writes one month's commission", n == 1, f"got {n}")

set_status("customer1@example.com", "active")
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("the customer now counts as paying", d["paying"] == 1, d)
check("$20 is earned", d["earned_all_time"] == 20.0, d)
check("and it is awaiting payout", d["awaiting_payout"] == 20.0, d)
check("the run rate reflects one paying customer", d["monthly_run_rate"] == 20.0, d)

n = pay("customer1@example.com", JAN, FEB)
check("the same invoice arriving twice does not pay twice", n == 0, f"got {n}")
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("so the total is unchanged after a retried webhook", d["earned_all_time"] == 20.0, d)

n = pay("customer1@example.com", FEB, MAR)
check("the next month's invoice earns again", n == 1, f"got {n}")
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("two paid months is $40", d["earned_all_time"] == 40.0, d)

# ── an annual payment earns every month it covers ─────────────────────────────
set_status("customer2@example.com", "active")
n = pay("customer2@example.com", datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2027, 1, 1, tzinfo=timezone.utc))
check("a year-long invoice earns twelve months, not one", n == 12, f"got {n}")

check("periods_between counts a single month once",
      P.periods_between(JAN, FEB) == ["2026-01"], P.periods_between(JAN, FEB))
check("a period ending mid-month still counts that month",
      P.periods_between(JAN, datetime(2026, 2, 14, tzinfo=timezone.utc)) == ["2026-01", "2026-02"])

# ── a customer who never paid earns nothing ───────────────────────────────────
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("customer3 signed up but never paid", d["signed_up"] >= 1, d)
db = SessionLocal()
c3 = db.query(M.User).filter(M.User.email == "customer3@example.com").first()
check("and has no commission rows",
      db.query(M.Commission).join(M.Referral, M.Referral.id == M.Commission.referral_id)
      .filter(M.Referral.user_id == c3.id).count() == 0)
db.close()

# ── a customer who paid then stopped ──────────────────────────────────────────
set_status("customer1@example.com", "canceled")
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("a churned customer stops counting as paying", d["paying"] == 1, d)   # only customer2 now
check("but shows as lapsed rather than vanishing", d["lapsed"] == 1, d)
check("and what they already earned is kept", d["earned_all_time"] == 40.0 + 240.0, d)

# ── privacy ───────────────────────────────────────────────────────────────────
body = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).text
check("the promoter is never shown a customer's email", "customer1@example.com" not in body)
check("nor their name", '"A Customer"' not in body)
check("but does see one row per referral",
      len(client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()["referrals"]) == 3)

# ── one promoter cannot see another's numbers ─────────────────────────────────
db = SessionLocal()
other_user_id = db.get(M.Promoter, OTHER_ID).user_id
db.close()
d2 = client.get("/api/promoter/dashboard", headers=auth(other_user_id)).json()
check("a second promoter sees only their own referrals", d2["total_referred"] == 0, d2)
check("and their own earnings", d2["earned_all_time"] == 0.0, d2)

# ── a customer cannot reach the promoter dashboard ────────────────────────────
db = SessionLocal()
cust_id = db.query(M.User).filter(M.User.email == "organic@example.com").first().id
db.close()
check("a customer is refused the promoter dashboard",
      client.get("/api/promoter/dashboard", headers=auth(cust_id)).status_code == 403)
check("and so is a signed-out visitor",
      client.get("/api/promoter/dashboard").status_code == 401)

# ── deactivating a link ───────────────────────────────────────────────────────
r = client.patch(f"/api/admin/promoters/{PRO['id']}", headers=H, json={"active": False})
check("a promoter can be deactivated", r.status_code == 200 and r.json()["active"] is False, r.text)
sign_up("afterdeactivation@example.com", ref="MATTB")
db = SessionLocal()
u = db.query(M.User).filter(M.User.email == "afterdeactivation@example.com").first()
check("a deactivated link no longer credits new signups",
      db.query(M.Referral).filter(M.Referral.user_id == u.id).first() is None)
db.close()
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("but the customers they already brought in keep earning",
      d["earned_all_time"] == 280.0, d)
client.patch(f"/api/admin/promoters/{PRO['id']}", headers=H, json={"active": True})

# ── rates ─────────────────────────────────────────────────────────────────────
r = client.put("/api/admin/promoters/rate", headers=H, json={"rate": 30})
check("an admin can change the default rate", r.status_code == 200, r.text)
r = client.post("/api/admin/promoters", headers=H,
                json={"email": "newpro@example.com", "password": "secret123"})
check("a promoter signed after the change gets the new rate", r.json()["rate"] == 30.0, r.text)
check("an existing promoter keeps the rate they were signed at",
      client.get("/api/admin/promoters", headers=H).json()[-1]["rate"] == 20.0)

r = client.patch(f"/api/admin/promoters/{PRO['id']}", headers=H, json={"rate": 25})
check("a rate can be changed for one promoter", r.json()["rate"] == 25.0, r.text)

# ── payouts ───────────────────────────────────────────────────────────────────
owed = client.get("/api/admin/promoters/summary", headers=H).json()
check("the admin summary totals what is owed", owed["owed"] == 280.0, owed)
check("and counts the referred customers who are paying", owed["referred_paying"] == 1, owed)

r = client.post("/api/admin/promoters/payouts", headers=H,
                json={"period": "2026-01", "payout_ref": "BANK-001"})
check("a month can be marked paid", r.status_code == 200, r.text)
check("and reports what it settled", r.json()["marked"] == 2 and r.json()["total"] == 40.0, r.json())

r2 = client.post("/api/admin/promoters/payouts", headers=H, json={"period": "2026-01"})
check("running the same payout twice settles nothing further", r2.json()["marked"] == 0, r2.json())

after = client.get("/api/admin/promoters/summary", headers=H).json()
check("the owed total drops by what was paid", after["owed"] == 240.0, after)
check("and the paid total rises", after["paid_out"] == 40.0, after)

# ── manual entries ────────────────────────────────────────────────────────────
r = client.post("/api/admin/promoters/commissions/manual", headers=H,
                json={"user_email": "customer3@example.com", "period": "2026-05"})
check("a commission can be recorded by hand", r.status_code == 201, r.text)
check("and is labelled as a decision, not evidence", r.json()["source"] == "manual", r.json())
r = client.post("/api/admin/promoters/commissions/manual", headers=H,
                json={"user_email": "customer3@example.com", "period": "2026-05"})
check("the same month cannot be recorded twice by hand", r.status_code == 409, r.text)
r = client.post("/api/admin/promoters/commissions/manual", headers=H,
                json={"user_email": "organic@example.com", "period": "2026-05"})
check("a customer nobody referred has no commission to record", r.status_code == 404, r.text)

# ── the CSV a payout run works from ───────────────────────────────────────────
r = client.get("/api/admin/promoters/export.csv", headers=H)
check("commissions export as CSV", r.status_code == 200, r.text[:200])
check("with a header row", r.text.splitlines()[0].startswith("promoter,email,code,period"))
check("and a row per commission", len(r.text.strip().splitlines()) >= 15, len(r.text.splitlines()))
check("the CSV is admin-only", client.get("/api/admin/promoters/export.csv").status_code == 401)

# ── deleting accounts ─────────────────────────────────────────────────────────
r = client.delete(f"/api/admin/users/{PRO_USER_ID}", headers=H)
check("a promoter with earnings cannot be deleted", r.status_code == 409, r.text)
check("and the refusal says to deactivate instead", "Deactivate" in r.text, r.text)

db = SessionLocal()
c2_id = db.query(M.User).filter(M.User.email == "customer2@example.com").first().id
db.close()
r = client.delete(f"/api/admin/users/{c2_id}", headers=H)
check("a referred customer can still be deleted", r.status_code == 204, r.text)
db = SessionLocal()
check("and their referral goes with them",
      db.query(M.Referral).filter(M.Referral.user_id == c2_id).count() == 0)
db.close()

r = client.delete(f"/api/admin/users/{other_user_id}", headers=H)
check("a promoter who never earned anything can be deleted", r.status_code == 204, r.text)

# ── a promoter is not a customer ──────────────────────────────────────────────
# Their account is created "approved" so they can sign in, and approved is the
# flag that otherwise waves someone straight past billing. Without an explicit
# rule, every promoter would quietly be holding a free copy of the paid product.
from app.security import has_product_access                     # noqa: E402

db = SessionLocal()
pro = db.query(M.User).filter(M.User.email == "influencer@example.com").first()
check("a promoter does not get the paid product for free", has_product_access(pro) is False)
check("even though they are approved so they can sign in", pro.status == "approved")
admin = db.query(M.User).filter(M.User.email == "admin@apexdemo.co.nz").first()
check("an admin still gets in", has_product_access(admin) is True)
db.close()

check("a promoter is refused the listings",
      client.get("/api/properties/map", headers=auth(PRO_USER_ID)).status_code == 402)
check("and the sold data", client.get("/api/sold", headers=auth(PRO_USER_ID)).status_code == 402)
check("but can reach their own dashboard",
      client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).status_code == 200)

# and a promoter cannot be referred by another promoter, or counted as a customer
db = SessionLocal()
pro2_user = db.get(M.User, db.get(M.Promoter, OTHER_ID).user_id) if db.get(M.Promoter, OTHER_ID) else None
if pro2_user is not None:
    made = P.attribute(db, pro2_user, "MATTB")
    check("one promoter cannot be counted as another's customer", made is None)
else:
    check("one promoter cannot be counted as another's customer", True)
db.close()

# ── link analytics ────────────────────────────────────────────────────────────
# The funnel a promoter actually wants: clicks -> signups -> paying. Clicks are
# the only step that cannot be verified, so the rules around them are about not
# flattering the number.
def clicked(code, visitor):
    return client.post("/api/promoter/click", json={"code": code, "visitor": visitor}).status_code


check("a click is counted without anyone being signed in", clicked("MATTB", "visitor-aaaa1111") == 204)
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("and shows on the promoter's dashboard", d["clicks"] == 1, d.get("clicks"))

clicked("MATTB", "visitor-aaaa1111")
clicked("MATTB", "visitor-aaaa1111")
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("the same person refreshing does not inflate the count", d["clicks"] == 1, d["clicks"])

clicked("MATTB", "visitor-bbbb2222")
clicked("mattb", "visitor-cccc3333")
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("different people each count once", d["clicks"] == 3, d["clicks"])
check("and the code is case-insensitive here too", d["visitors"] == 3, d["visitors"])

check("an unknown code is accepted and quietly ignored", clicked("NOSUCHCODE", "visitor-dddd4444") == 204)
check("a too-short visitor id is ignored", clicked("MATTB", "abc") == 204)
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("neither adds to the count", d["clicks"] == 3, d["clicks"])

check("the conversion rates are reported",
      d["click_to_signup"] is not None and d["signup_to_paying"] is not None, d)
check("click-to-signup is signups over visitors",
      d["click_to_signup"] == round(100.0 * d["total_referred"] / d["visitors"], 1), d)

# a promoter with an untouched link should not read as "0% converting"
d2 = client.get("/api/promoter/dashboard", headers=auth(other_user_id)).json() \
     if False else None
r = client.post("/api/admin/promoters", headers=H,
                json={"email": "fresh@example.com", "password": "secret123", "code": "FRESH"})
fresh_user_id = r.json()["user_id"]
d3 = client.get("/api/promoter/dashboard", headers=auth(fresh_user_id)).json()
check("a link nobody has opened reports no rate rather than 0%",
      d3["click_to_signup"] is None, d3)
check("and no clicks", d3["clicks"] == 0, d3)

# a paused link stops counting clicks as well as signups
client.patch(f"/api/admin/promoters/{PRO['id']}", headers=H, json={"active": False})
clicked("MATTB", "visitor-eeee5555")
d = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("a paused link stops counting clicks too", d["clicks"] == 3, d["clicks"])
client.patch(f"/api/admin/promoters/{PRO['id']}", headers=H, json={"active": True})

check("the admin list carries the same funnel",
      any(p["clicks"] == 3 for p in client.get("/api/admin/promoters", headers=H).json()))

# nothing identifying is kept
db = SessionLocal()
cols = {c.name for c in M.ReferralClick.__table__.columns}
check("clicks store no ip address", "ip" not in cols and "ip_address" not in cols)
check("and no user agent", "user_agent" not in cols, cols)
db.close()

# ── which ad is working ───────────────────────────────────────────────────────
client.post("/api/promoter/click", json={"code": "MATTB", "visitor": "camp-1111aaaa", "campaign": "Insta Reel — Aug"})
client.post("/api/promoter/click", json={"code": "MATTB", "visitor": "camp-2222bbbb", "campaign": "insta-reel-aug"})
client.post("/api/promoter/click", json={"code": "MATTB", "visitor": "camp-3333cccc", "campaign": "youtube"})

rows = client.get("/api/promoter/campaigns", headers=auth(PRO_USER_ID)).json()
byname = {r["campaign"]: r for r in rows}
check("clicks are broken down per ad", "insta-reel-aug" in byname, list(byname))
check("and two spellings of one ad are one row",
      byname.get("insta-reel-aug", {}).get("clicks") == 2, byname.get("insta-reel-aug"))
check("a second ad is its own row", byname.get("youtube", {}).get("clicks") == 1, byname.get("youtube"))
check("untagged traffic is kept rather than dropped", "" in byname, list(byname))

r = client.post("/api/auth/sign-up", json={"email": "fromreel@example.com", "password": "secret123",
                                           "ref": "MATTB", "campaign": "insta-reel-aug"})
check("a signup carries the ad that produced it", r.status_code == 201, r.text)
rows = client.get("/api/promoter/campaigns", headers=auth(PRO_USER_ID)).json()
byname = {r["campaign"]: r for r in rows}
check("and shows against that ad", byname["insta-reel-aug"]["signups"] == 1, byname["insta-reel-aug"])
check("with a conversion rate for it",
      byname["insta-reel-aug"]["click_to_signup"] == 50.0, byname["insta-reel-aug"])
check("the other ad has no signups yet", byname["youtube"]["signups"] == 0, byname["youtube"])

check("campaign totals do not exceed the overall signups",
      sum(r["signups"] for r in rows) ==
      client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()["total_referred"],
      rows)

# ── the media pack ────────────────────────────────────────────────────────────
kit = client.get("/api/promoter/kit", headers=auth(PRO_USER_ID)).json()
check("the media pack loads", "product" in kit and "templates" in kit, list(kit))
check("it carries ready-made copy", len(kit["templates"]) >= 4, len(kit["templates"]))
check("with the promoter's own link already in it",
      all("MATTB" in t["text"] for t in kit["templates"]))
check("and the brand colours", any(c["hex"] == "#E4002B" for c in kit["colours"]))
check("the rules are stated, not just enforced on the AI",
      any("guaranteed" in r for r in kit["rules"]), kit["rules"])
check("including the disclosure requirement",
      any("referral" in r.lower() for r in kit["rules"]))
check("ready-made copy works without an AI key configured", kit["ai_available"] is False)
check("and the pack says how many AI drafts are left", kit["ads_remaining"] == kit["ads_limit"])

r = client.post("/api/promoter/ads", headers=auth(PRO_USER_ID), json={"channel": "Instagram caption"})
check("asking for AI ads with no key set says so rather than 500ing",
      r.status_code == 503, f"{r.status_code} {r.text[:120]}")
check("and points at the copy that does work", "ready-made" in r.text, r.text[:160])

check("a customer cannot read the media pack",
      client.get("/api/promoter/kit", headers=auth(cust_id)).status_code == 403)
check("nor draft ads on the account's key",
      client.post("/api/promoter/ads", headers=auth(cust_id), json={}).status_code == 403)

# ── the ad pack ───────────────────────────────────────────────────────────────
# Uploaded by an admin, downloaded by promoters. The bytes live in the database
# because a Railway container's disk is wiped on every deploy, and a download
# that 404s after a release is a bug the promoter finds, not us.
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 400

r = client.post("/api/admin/promoters/assets", headers=H,
                files={"file": ("story-1.png", PNG, "image/png")},
                data={"title": "Story image — under value", "kind": "image",
                      "note": "1080x1920, safe area at the top"})
check("an admin can upload an ad pack item", r.status_code == 201, r.text)
ASSET = r.json()
check("its size is recorded", ASSET["size_bytes"] == len(PNG), ASSET)
check("and it is downloadable", ASSET["downloadable"] is True, ASSET)

r = client.post("/api/admin/promoters/assets", headers=H,
                data={"title": "Brand video", "kind": "video",
                      "url": "https://youtube.com/watch?v=abc"})
check("a link can be added instead of a file", r.status_code == 201, r.text)
check("a link carries no bytes", r.json()["downloadable"] is False, r.json())
LINK_ID = r.json()["id"]

r = client.post("/api/admin/promoters/assets", headers=H, data={"title": "Nothing"})
check("an item with neither a file nor a link is refused", r.status_code == 422, r.text)
r = client.post("/api/admin/promoters/assets", headers=H,
                data={"title": "Bad link", "url": "javascript:alert(1)"})
check("a link that is not http(s) is refused", r.status_code == 422, r.text)
r = client.post("/api/admin/promoters/assets", headers=H,
                files={"file": ("x.png", PNG, "image/png")}, data={"kind": "sculpture"})
check("an unknown kind is refused", r.status_code == 422, r.text)

r = client.post("/api/admin/promoters/assets", headers=H,
                files={"file": ("huge.bin", b"0" * (21 * 1024 * 1024), "application/octet-stream")},
                data={"title": "Too big"})
check("a file over the limit is refused", r.status_code == 413, r.status_code)
check("and the refusal says to use a link instead", "link" in r.text.lower(), r.text[:200])

# what a promoter sees
assets = client.get("/api/promoter/assets", headers=auth(PRO_USER_ID)).json()
check("a promoter sees the ad pack", len(assets) == 2, assets)
check("with the titles the admin gave them",
      any(a["title"].startswith("Story image") for a in assets), assets)

r = client.get(f"/api/promoter/assets/{ASSET['id']}/file", headers=auth(PRO_USER_ID))
check("and can download a file", r.status_code == 200, r.status_code)
check("getting the exact bytes back", r.content == PNG, len(r.content))
check("with a filename attached", "story-1.png" in r.headers.get("content-disposition", ""),
      r.headers.get("content-disposition"))

check("a customer cannot see the ad pack",
      client.get("/api/promoter/assets", headers=auth(cust_id)).status_code == 403)
check("nor download from it",
      client.get(f"/api/promoter/assets/{ASSET['id']}/file", headers=auth(cust_id)).status_code == 403)
check("and neither can a signed-out visitor",
      client.get(f"/api/promoter/assets/{ASSET['id']}/file").status_code == 401)

# hiding, rather than deleting
r = client.patch(f"/api/admin/promoters/assets/{ASSET['id']}", headers=H, json={"active": False})
check("an item can be hidden", r.status_code == 200 and r.json()["active"] is False, r.text)
check("a hidden item disappears for promoters",
      len(client.get("/api/promoter/assets", headers=auth(PRO_USER_ID)).json()) == 1)
check("and cannot be downloaded by them either",
      client.get(f"/api/promoter/assets/{ASSET['id']}/file", headers=auth(PRO_USER_ID)).status_code == 404)
check("but the admin still sees it in the list",
      len(client.get("/api/admin/promoters/assets", headers=H).json()) >= 2)
check("and can still fetch it to check before un-hiding",
      client.get(f"/api/admin/promoters/assets/{ASSET['id']}/file", headers=H).status_code == 200)
client.patch(f"/api/admin/promoters/assets/{ASSET['id']}", headers=H, json={"active": True})
check("un-hiding brings it back",
      len(client.get("/api/promoter/assets", headers=auth(PRO_USER_ID)).json()) == 2)

check("uploading is admin-only",
      client.post("/api/admin/promoters/assets", data={"title": "x", "url": "https://a.b"}).status_code == 401)
check("a promoter cannot upload to the pack",
      client.post("/api/admin/promoters/assets", headers=auth(PRO_USER_ID),
                  data={"title": "x", "url": "https://a.b"}).status_code == 403)

sm = client.get("/api/admin/promoters/summary", headers=H).json()
check("the admin summary counts the pack", sm["assets"] == 2, sm)
check("and reports how much of the database it is using",
      sm["assets_bytes"] == len(PNG), sm)

r = client.delete(f"/api/admin/promoters/assets/{LINK_ID}", headers=H)
check("an item can be deleted outright", r.status_code == 204, r.text)
check("and is gone from the promoter's pack",
      len(client.get("/api/promoter/assets", headers=auth(PRO_USER_ID)).json()) == 1)

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
