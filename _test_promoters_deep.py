"""The referral programme, function by function.

The first suite drives the API the way a user does. This one goes underneath it:
the Stripe webhook handler, the ad generator's parsing, the date arithmetic, and
the places where two numbers on the same screen are computed two different ways
and can therefore disagree.

That last category is the one worth the effort. A test that an endpoint returns
200 catches an outage. A test that the per-customer earnings add up to the total
earnings catches a promoter reading two different figures for the same money,
which is the failure that costs trust rather than uptime.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/_test_promoters_deep.db")
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

from app import checkout                           # noqa: E402
from app import models as M                        # noqa: E402
from app import promo_kit                          # noqa: E402
from app import promoters as P                     # noqa: E402
from app.assistant import providers                # noqa: E402
from app.db import Base, SessionLocal, engine      # noqa: E402
from app.main import app                           # noqa: E402
from app.security import create_access_token, hash_password  # noqa: E402

Base.metadata.create_all(engine)
client = TestClient(app)


def wipe():
    db = SessionLocal()
    for model in (M.Commission, M.ReferralClick, M.Referral, M.Promoter,
                  M.PromoAsset, M.WishList, M.VerificationCode, M.AssistantLog):
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


ADMIN_ID = wipe()
H = {"Authorization": f"Bearer {create_access_token(ADMIN_ID)[0]}"}
auth = lambda uid: {"Authorization": f"Bearer {create_access_token(uid)[0]}"}  # noqa: E731

# ══ 1. the date arithmetic that decides how many months get paid ═════════════
UTC = timezone.utc
d = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731

check("one calendar month is one period",
      P.periods_between(d(2026, 3, 1), d(2026, 4, 1)) == ["2026-03"],
      P.periods_between(d(2026, 3, 1), d(2026, 4, 1)))
check("a period ending mid-month covers both months",
      P.periods_between(d(2026, 3, 15), d(2026, 4, 14)) == ["2026-03", "2026-04"],
      P.periods_between(d(2026, 3, 15), d(2026, 4, 14)))
check("a year is twelve periods",
      len(P.periods_between(d(2026, 1, 1), d(2027, 1, 1))) == 12)
check("a year crossing New Year is still twelve",
      len(P.periods_between(d(2026, 7, 1), d(2027, 7, 1))) == 12,
      P.periods_between(d(2026, 7, 1), d(2027, 7, 1)))
check("and it starts and ends in the right months",
      P.periods_between(d(2026, 7, 1), d(2027, 7, 1))[0] == "2026-07"
      and P.periods_between(d(2026, 7, 1), d(2027, 7, 1))[-1] == "2027-06")
check("dates the wrong way round are handled, not crashed",
      P.periods_between(d(2026, 4, 1), d(2026, 3, 1)) == ["2026-03"],
      P.periods_between(d(2026, 4, 1), d(2026, 3, 1)))
check("identical start and end still bills one month",
      P.periods_between(d(2026, 5, 9), d(2026, 5, 9)) == ["2026-05"])
check("an absurd period is clamped rather than billing forever",
      len(P.periods_between(d(2020, 1, 1), d(2030, 1, 1))) <= 36,
      len(P.periods_between(d(2020, 1, 1), d(2030, 1, 1))))

# ══ 2. code and campaign normalisation ═══════════════════════════════════════
check("a code is uppercased", P.normalise_code("mattb") == "MATTB")
check("punctuation is stripped from a code", P.normalise_code(" matt-b! ") == "MATTB")
check("an empty code is empty, not None-crashing", P.normalise_code(None) == "")
check("a code is capped in length", len(P.normalise_code("A" * 90)) == 32)

check("a campaign is lowercased and dashed", P.clean_campaign("Insta Reel — Aug") == "insta-reel-aug")
check("runs of dashes collapse", P.clean_campaign("a---b") == "a-b")
check("leading and trailing dashes go", P.clean_campaign("--x--") == "x")
check("an empty campaign is None, not an empty string",
      P.clean_campaign("   ") is None, P.clean_campaign("   "))
check("a campaign is capped in length", len(P.clean_campaign("z" * 90) or "") == 40)

# ══ 3. setup for the money tests ═════════════════════════════════════════════
pro = client.post("/api/admin/promoters", headers=H,
                  json={"email": "deep@example.com", "password": "secret123",
                        "code": "DEEP", "rate": 20}).json()
PRO_ID = pro["id"]
db = SessionLocal()
PRO_USER_ID = db.query(M.User).filter(M.User.email == "deep@example.com").first().id
db.close()

client.post("/api/auth/sign-up", json={"email": "buyer1@example.com", "password": "secret123",
                                       "ref": "DEEP", "campaign": "reel"})
db = SessionLocal()
BUYER = db.query(M.User).filter(M.User.email == "buyer1@example.com").first()
BUYER_ID = BUYER.id
BUYER.stripe_customer_id = "cus_deep_1"
BUYER.subscription_status = "active"
db.commit(); db.close()

# ══ 4. the Stripe webhook path — the one that actually pays people ═══════════
db = SessionLocal()
inv = {
    "id": "in_test_1",
    "customer": "cus_deep_1",
    "lines": {"data": [{"period": {"start": int(d(2026, 3, 1).timestamp()),
                                   "end": int(d(2026, 4, 1).timestamp())}}]},
}
checkout._credit_referrer(db, inv)
n = db.query(M.Commission).count()
check("a paid invoice from Stripe writes a commission", n == 1, f"got {n}")
check("for the month the invoice covers",
      db.query(M.Commission).first().period == "2026-03",
      db.query(M.Commission).first().period)
check("marked as coming from an invoice", db.query(M.Commission).first().source == "invoice")
check("and the customer is stamped as having paid",
      db.query(M.Referral).filter(M.Referral.user_id == BUYER_ID).first().first_paid_at is not None)

checkout._credit_referrer(db, inv)
check("the same invoice replayed pays nothing more", db.query(M.Commission).count() == 1)

checkout._credit_referrer(db, {"id": "in_x", "customer": "cus_nobody",
                               "lines": {"data": []}})
check("an invoice for an unknown customer is ignored", db.query(M.Commission).count() == 1)

checkout._credit_referrer(db, {"id": "in_broken"})
check("a malformed invoice does not raise", db.query(M.Commission).count() == 1)

# an invoice with no line period at all falls back to its created date
checkout._credit_referrer(db, {"id": "in_2", "customer": "cus_deep_1",
                               "created": int(d(2026, 6, 12).timestamp()), "lines": {"data": []}})
check("an invoice with no period still bills its own month",
      db.query(M.Commission).filter(M.Commission.period == "2026-06").count() == 1,
      [c.period for c in db.query(M.Commission).all()])
db.close()

# ══ 5. two numbers for the same money must agree ═════════════════════════════
# The dashboard totals commissions from the ledger; the per-customer rows are
# computed separately. If one uses the recorded amount and the other multiplies
# by the CURRENT rate, they part company the moment a rate changes — and the
# promoter is looking at both at once.
client.patch(f"/api/admin/promoters/{PRO_ID}", headers=H, json={"rate": 35})

dash = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
row_total = round(sum(r["earned"] for r in dash["referrals"]), 2)
check("after a rate change, the per-customer rows still add up to the total",
      row_total == dash["earned_all_time"],
      f"rows={row_total} total={dash['earned_all_time']}")

camps = client.get("/api/promoter/campaigns", headers=auth(PRO_USER_ID)).json()
camp_total = round(sum(c["earned"] for c in camps), 2)
check("and so does the per-ad breakdown",
      camp_total == dash["earned_all_time"],
      f"campaigns={camp_total} total={dash['earned_all_time']}")

check("what was already earned is not repriced by the change",
      dash["earned_all_time"] == 40.0, dash["earned_all_time"])

# the NEW rate applies to the next month paid
db = SessionLocal()
u = db.query(M.User).filter(M.User.id == BUYER_ID).first()
P.record_payment(db, u, d(2026, 9, 1), d(2026, 10, 1))
newest = (db.query(M.Commission).filter(M.Commission.period == "2026-09").first())
check("a month paid after the change uses the new rate", newest.amount == 35.0, newest.amount)
db.close()

dash = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("and the totals still reconcile afterwards",
      round(sum(r["earned"] for r in dash["referrals"]), 2) == dash["earned_all_time"],
      dash)
client.patch(f"/api/admin/promoters/{PRO_ID}", headers=H, json={"rate": 20})

# ══ 6. clicks ════════════════════════════════════════════════════════════════
db = SessionLocal()
check("a click on an unknown code is not recorded",
      P.record_click(db, "NOPE", "visitor-deep-0001") is False)
check("a first click is recorded", P.record_click(db, "DEEP", "visitor-deep-0001") is True)
check("the same visitor the same day is not", P.record_click(db, "DEEP", "visitor-deep-0001") is False)

# the same person tomorrow IS a new visit, but still one unique visitor
row = db.query(M.ReferralClick).filter(M.ReferralClick.visitor == "visitor-deep-0001").first()
db.add(M.ReferralClick(promoter_id=row.promoter_id, visitor="visitor-deep-0001",
                       day=(datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")))
db.commit()
counts = P.click_counts(db, PRO_ID)
check("two days of visits are two rows", counts["clicks"] == 2, counts)
check("but one person", counts["visitors"] == 1, counts)
db.close()

dash = client.get("/api/promoter/dashboard", headers=auth(PRO_USER_ID)).json()
check("the funnel uses people, not visits, for the conversion rate",
      dash["click_to_signup"] == round(100.0 * dash["total_referred"] / dash["visitors"], 1), dash)

check("_pct returns None rather than 0 when nothing has happened", P._pct(0, 0) is None)
check("_pct is a percentage", P._pct(1, 4) == 25.0)

# ══ 7. the ad generator's parsing ════════════════════════════════════════════
class FakeResult:
    def __init__(self, text):
        self.text = text


ORIGINAL_RUN = providers.run


def fake_run_factory(payload):
    def _run(**kwargs):
        return FakeResult(payload)
    return _run


db = SessionLocal()
promoter_row = db.get(M.Promoter, PRO_ID)

providers.run = fake_run_factory(
    '[{"channel":"Instagram","hook":"h1","text":"t1"},'
    ' {"channel":"Instagram","hook":"h2","text":"t2"}]')
out = promo_kit.generate_ads(db, promoter_row, "https://x/y", channel="Instagram",
                             angle="", provider="anthropic", api_key="k")
check("a clean JSON array parses into drafts", len(out) == 2, out)
check("with the fields the UI needs", out[0]["hook"] == "h1" and out[0]["text"] == "t1", out)

providers.run = fake_run_factory(
    'Sure! Here you go:\n```json\n[{"channel":"X","hook":"a","text":"b"}]\n```\nHope that helps.')
out = promo_kit.generate_ads(db, promoter_row, "https://x/y", channel="X",
                             angle="", provider="anthropic", api_key="k")
check("JSON wrapped in chat and a code fence still parses", len(out) == 1, out)

providers.run = fake_run_factory("I could not do that, sorry.")
out = promo_kit.generate_ads(db, promoter_row, "https://x/y", channel="X",
                             angle="", provider="anthropic", api_key="k")
check("unparseable output becomes one draft rather than an error", len(out) == 1, out)
check("carrying whatever the model did say", "could not" in out[0]["text"], out)

providers.run = fake_run_factory('[{"channel":"X"}, "not an object", {"text":"ok"}]')
out = promo_kit.generate_ads(db, promoter_row, "https://x/y", channel="X",
                             angle="", provider="anthropic", api_key="k")
check("junk entries in the array are dropped, not rendered", len(out) == 2, out)

providers.run = fake_run_factory("[" + ",".join(
    '{"channel":"X","hook":"h","text":"t"}' for _ in range(20)) + "]")
out = promo_kit.generate_ads(db, promoter_row, "https://x/y", channel="X",
                             angle="", provider="anthropic", api_key="k")
check("a model that returns twenty drafts is capped", len(out) <= 5, len(out))

# the system prompt must actually carry the rules
seen = {}


def capture_run(**kwargs):
    seen.update(kwargs)
    return FakeResult('[{"channel":"X","hook":"h","text":"t"}]')


providers.run = capture_run
promo_kit.generate_ads(db, promoter_row, "https://apex/sign-up?ref=DEEP",
                       channel="TikTok", angle="for renters", provider="anthropic", api_key="k")
check("the model is told not to promise returns",
      "guaranteed" in seen["system"].lower(), seen.get("system", "")[:200])
check("and not to claim it is financial advice",
      "financial" in seen["system"].lower())
check("and not to invent statistics",
      "invent" in seen["system"].lower())
check("the promoter's own link is passed to the model",
      "ref=DEEP" in seen["messages"][0]["content"], seen["messages"])
check("along with the channel they asked for",
      "TikTok" in seen["messages"][0]["content"])
check("and their angle", "renters" in seen["messages"][0]["content"])

providers.run = ORIGINAL_RUN
db.close()

# ══ 8. the ad-copy daily cap ═════════════════════════════════════════════════
db = SessionLocal()
for _ in range(promo_kit.DAILY_ADS):
    db.add(M.AssistantLog(user_id=PRO_USER_ID, question="[ad-copy] x", answer="y",
                          ok=True, region="ad-copy"))
db.commit()
used = promo_kit.ads_used_today(db, PRO_USER_ID)
check("ad drafts are counted against a daily cap", used == promo_kit.DAILY_ADS, used)
db.add(M.AssistantLog(user_id=PRO_USER_ID, question="a real question", answer="y",
                      ok=True, region="Auckland"))
db.commit()
check("Ask Ollie questions do not count against the ad cap",
      promo_kit.ads_used_today(db, PRO_USER_ID) == promo_kit.DAILY_ADS)
db.close()

r = client.post("/api/promoter/ads", headers=auth(PRO_USER_ID), json={"channel": "Instagram caption"})
check("at the cap, drafting is refused with 429", r.status_code == 429, f"{r.status_code} {r.text[:120]}")
check("and the refusal points at the copy that still works",
      "ready-made" in r.text.lower(), r.text[:160])

kit = client.get("/api/promoter/kit", headers=auth(PRO_USER_ID)).json()
check("the media pack reports none left", kit["ads_remaining"] == 0, kit["ads_remaining"])
check("but still serves the templates", len(kit["templates"]) >= 4)

db = SessionLocal()
db.query(M.AssistantLog).filter(M.AssistantLog.user_id == PRO_USER_ID).delete()
db.commit(); db.close()

# ══ 9. the templates themselves ══════════════════════════════════════════════
kit = client.get("/api/promoter/kit", headers=auth(PRO_USER_ID)).json()
for t in kit["templates"]:
    check(f"template {t['channel']!r} carries the promoter's link", "ref=DEEP" in t["text"])
check("every template discloses the referral",
      all(("#ad" in t["text"] or "referral" in t["text"].lower() or "paid" in t["text"].lower())
          for t in kit["templates"]),
      [t["channel"] for t in kit["templates"]
       if not ("#ad" in t["text"] or "referral" in t["text"].lower() or "paid" in t["text"].lower())])
check("no template promises a return",
      not any("guaranteed" in t["text"].lower() for t in kit["templates"]))
check("and every one mentions the trial needs a card or can be cancelled",
      all(("card" in t["text"].lower() or "cancel" in t["text"].lower())
          for t in kit["templates"]),
      [t["channel"] for t in kit["templates"]
       if not ("card" in t["text"].lower() or "cancel" in t["text"].lower())])

# ══ 10. assets: the awkward combinations ═════════════════════════════════════
r = client.post("/api/admin/promoters/assets", headers=H,
                files={"file": ("both.png", b"\x89PNG\r\n\x1a\n" + b"x" * 50, "image/png")},
                data={"title": "Both", "kind": "image", "url": "https://example.com/x"})
check("an item can have a file and a link at once", r.status_code == 201, r.text)
check("and is downloadable from us", r.json()["downloadable"] is True, r.json())
check("while still carrying the link", r.json()["url"] == "https://example.com/x", r.json())
BOTH_ID = r.json()["id"]

r = client.get(f"/api/promoter/assets/{BOTH_ID}/file", headers=auth(PRO_USER_ID))
check("the file comes back with its own content type",
      r.headers.get("content-type", "").startswith("image/png"), r.headers.get("content-type"))

r = client.post("/api/admin/promoters/assets", headers=H,
                files={"file": ("empty.png", b"", "image/png")}, data={"title": "Empty"})
check("an empty file is refused", r.status_code == 422, r.status_code)

r = client.get("/api/promoter/assets/999999/file", headers=auth(PRO_USER_ID))
check("a missing asset is a 404, not a 500", r.status_code == 404, r.status_code)

# ══ 11. payouts, scoped ══════════════════════════════════════════════════════
second = client.post("/api/admin/promoters", headers=H,
                     json={"email": "second@example.com", "password": "secret123", "code": "SECOND"}).json()
client.post("/api/auth/sign-up", json={"email": "buyer2@example.com", "password": "secret123", "ref": "SECOND"})
db = SessionLocal()
b2 = db.query(M.User).filter(M.User.email == "buyer2@example.com").first()
b2.subscription_status = "active"
db.commit()
P.record_payment(db, b2, d(2026, 3, 1), d(2026, 4, 1))
db.close()

r = client.post("/api/admin/promoters/payouts", headers=H,
                json={"period": "2026-03", "promoter_id": PRO_ID, "payout_ref": "ONLY-ONE"})
check("a payout can be scoped to one promoter", r.json()["marked"] == 1, r.json())
check("and only settles that promoter's amount", r.json()["total"] == 20.0, r.json())

others = [c for c in client.get("/api/admin/promoters/commissions", headers=H).json()
          if c["promoter_id"] == second["id"]]
check("the other promoter is untouched", all(c["paid_at"] is None for c in others), others)

# ══ 12. the CSV a payout run is done from ════════════════════════════════════
csv_text = client.get("/api/admin/promoters/export.csv", headers=H).text
lines = csv_text.strip().splitlines()
check("the CSV has a row per commission",
      len(lines) - 1 == len(client.get("/api/admin/promoters/commissions", headers=H).json()),
      f"{len(lines) - 1} rows")
check("it names the promoter's email", "deep@example.com" in csv_text)
check("it carries the payout reference once paid", "ONLY-ONE" in csv_text)
check("and the period", "2026-03" in csv_text)

# ══ 13. deleting, with clicks in the way ═════════════════════════════════════
r = client.post("/api/admin/promoters", headers=H,
                json={"email": "third@example.com", "password": "secret123", "code": "THIRD"})
third_user = r.json()["user_id"]
client.post("/api/promoter/click", json={"code": "THIRD", "visitor": "visitor-third-0001"})
r = client.delete(f"/api/admin/users/{third_user}", headers=H)
check("a promoter with clicks but no earnings can still be deleted",
      r.status_code == 204, r.text)
db = SessionLocal()
check("and their clicks go with them",
      db.query(M.ReferralClick).filter(M.ReferralClick.visitor == "visitor-third-0001").count() == 0)
db.close()

# ══ 14. a paused promoter's existing customers keep paying ═══════════════════
client.patch(f"/api/admin/promoters/{PRO_ID}", headers=H, json={"active": False})
db = SessionLocal()
u = db.query(M.User).filter(M.User.id == BUYER_ID).first()
made = P.record_payment(db, u, d(2026, 11, 1), d(2026, 12, 1))
db.close()
check("a paused promoter still earns on customers they already brought in",
      len(made) == 1, made)
client.patch(f"/api/admin/promoters/{PRO_ID}", headers=H, json={"active": True})

# ══ 15. a payment from someone nobody referred ═══════════════════════════════
# The orphan case the guard in record_payment defends against cannot actually
# happen — promoter_id is a foreign key, so a referral cannot point at a
# promoter that is gone. What CAN happen every day is an ordinary customer
# paying, and that must cost nothing and raise nothing.
db = SessionLocal()
nobody = M.User(email="nobody@example.com", password_hash=hash_password("secret123"),
                role="user", status="pending", stripe_customer_id="cus_nobody_x")
db.add(nobody); db.commit(); db.refresh(nobody)
before = db.query(M.Commission).count()
made = P.record_payment(db, nobody, d(2026, 3, 1), d(2026, 4, 1))
check("a customer nobody referred earns nobody anything", made == [], made)
check("and writes no rows", db.query(M.Commission).count() == before)

checkout._credit_referrer(db, {"id": "in_org", "customer": "cus_nobody_x",
                               "lines": {"data": []}})
check("the same through the webhook path", db.query(M.Commission).count() == before)
db.close()

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
