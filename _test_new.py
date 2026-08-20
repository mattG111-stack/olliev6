"""Focused tests for this round: the subdivision sanity guard (reputation fix)
and the market-history endpoint."""
import sys

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   -- {detail}" if detail and not cond else ""))

from app.main import app
from app.db import Base, engine, SessionLocal
import app.models as M
from app.security import create_access_token
from app.pricing.subdivision import compute
from datetime import datetime, timezone, timedelta
Base.metadata.create_all(engine)


def reset_users(db):
    """Clear every account and everything that points at one, children first.

    Foreign keys are enforced now (see app/db.py), as they always were in
    production. Deleting the parent first fails — which is the same fault as the
    bug this enforcement exists to catch: a row left pointing at a record that no
    longer exists.
    """
    for t in (M.AssistantLog, M.WishList, M.VerificationCode, M.AgentContact,
              M.AppSetting, M.IngestJob, M.User):
        db.query(t).delete()
    db.commit()


Z = "Residential - Mixed Housing Urban Zone"

# ---------------------------------------------------------------- 1) the guard
bad = compute(zone=Z, land_area=2243.0, buy_price=1_282_500, section_rate=4000,
              rate_source="section_rate", address="5 Rangeview Road", property_type="House",
              title_type="Freehold", improvement_value=300_000, cv=600_000)
check("implausible: flag set", bad.implausible is True)
check("implausible: gross_sales blanked", bad.gross_sales is None, f"got {bad.gross_sales}")
check("implausible: profit blanked", bad.subdivision_profit is None, f"got {bad.subdivision_profit}")
check("implausible: best_net_gain blanked", bad.best_net_gain is None, f"got {bad.best_net_gain}")
check("implausible: total_subdivided_value blanked", bad.total_subdivided_value is None,
      f"got {bad.total_subdivided_value}")
check("implausible: NOT flagged an opportunity", bad.is_subdividable is False)

ok = compute(zone=Z, land_area=2243.0, buy_price=1_282_500, section_rate=1200,
             rate_source="section_rate", address="OK St", property_type="House",
             title_type="Freehold", improvement_value=300_000, cv=3_500_000)
check("credible: flag NOT set", ok.implausible is False)
check("credible: figures still published", ok.gross_sales is not None and ok.subdivision_profit is not None,
      f"gross={ok.gross_sales} profit={ok.subdivision_profit}")
print(f"      (credible site keeps its workings: gross=${ok.gross_sales:,} profit=${ok.subdivision_profit:,})")

# ---------------------------------------------------------------- seed a DB
db = SessionLocal()
# Children before parents. With foreign keys enforced (as production enforces
# them) an incomplete reset fails on the parent delete — which is the same shape
# as the bug that shipped: a row left pointing at something that no longer exists.
for t in (M.AssistantLog, M.WishList, M.VerificationCode, M.AgentContact,
          M.AppSetting, M.IngestJob, M.PropertyForSale, M.PropertySold,
          M.PropertyRent, M.ImportBatch, M.User):
    db.query(t).delete()
db.commit()
admin = M.User(email="a@b.c", password_hash="$2b$12$" + "x" * 53, role="admin", status="approved")
db.add(admin); db.commit(); db.refresh(admin); aid = admin.id

base = datetime(2026, 7, 1, tzinfo=timezone.utc)
batches = []
for w in range(3):                       # 3 published weekly snapshots
    b = M.ImportBatch(batch_type="for_sale", region="Auckland", filename=f"w{w}.csv",
                      is_active=(w == 2), status="published", published_at=base + timedelta(weeks=w))
    db.add(b); db.commit(); db.refresh(b); batches.append(b)
    for i in range(4):
        db.add(M.PropertyForSale(
            import_batch_id=b.id, region="Auckland", suburb="Remuera", address=f"{i} Real St",
            asking_price=900_000 + w * 20_000, cv_numeric=1_000_000, fair_value=1_100_000,
            market_value=855_000, floor_area_m2=140, land_area_m2=600, is_held=False,
            is_underpriced=True, is_subdividable=(i == 0), subdivision_profit=(250_000 if i == 0 else None),
            best_net_gain=(250_000 if i == 0 else None), opportunity_score_pct=80.0,
            beds=3, baths=2, margin=0.22, predicted_days=40 - w * 3))
    # a Rangeview-style row in the LIVE batch: held-out figures must stay blank
    if w == 2:
        db.add(M.PropertyForSale(
            import_batch_id=b.id, region="Auckland", suburb="Sunnyvale", address="5 Rangeview Road",
            asking_price=1_350_000, cv_numeric=1_750_000, fair_value=1_724_305, floor_area_m2=144,
            land_area_m2=2243, is_held=False, is_subdividable=False,
            subdivision_profit=None, best_net_gain=None, gross_sales=None,
            opportunity_score_pct=50.0, beds=3, baths=1))
db.commit(); db.close()

from fastapi.testclient import TestClient
client = TestClient(app)
H = {"Authorization": f"Bearer {create_access_token(aid)[0]}"}

# ---------------------------------------------------------------- 2) market history
r = client.get("/api/dashboards/market-history", headers=H)
check("GET /market-history 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
if r.status_code == 200:
    pts = r.json()["points"]
    check("market-history returns a point per published week", len(pts) == 3, f"got {len(pts)}")
    check("points are oldest-first (plottable L->R)",
          [p["batch_date"] for p in pts] == sorted(p["batch_date"] for p in pts))
    check("median asking rises across the series",
          pts[0]["median_asking"] < pts[-1]["median_asking"],
          f'{pts[0]["median_asking"]} -> {pts[-1]["median_asking"]}')
    check("each point carries listings + days", all(p["listing_count"] > 0 for p in pts))
    for p in pts:
        print(f"      {p['batch_date']}  n={p['listing_count']}  ask=${p['median_asking']:,.0f}  days={p['median_days_to_sell']}")

# ---------------------------------------------------------------- 3) no leak anywhere
r = client.get("/api/dashboards/headline", headers=H)
check("GET /headline 200", r.status_code == 200)
if r.status_code == 200:
    j = r.json()
    check("headline subdivision total excludes the suppressed site",
          j.get("subdivision_profit_total") == 250_000, f"got {j.get('subdivision_profit_total')}")
    check("headline subdividable count excludes it", j.get("subdividable") == 1, f"got {j.get('subdividable')}")

r = client.get("/api/properties?subdividable=true", headers=H)
check("suppressed site absent from the subdividable feed",
      r.status_code == 200 and not any("Rangeview" in (x.get("address") or "") for x in r.json()["rows"]))

r = client.get("/api/properties/export.csv", headers=H)
if r.status_code == 200:
    line = next((l for l in r.text.splitlines() if "Rangeview" in l), "")
    check("suppressed site exports with NO profit figure", line != "" and "3027532" not in line and "11812500" not in line,
          f"row: {line[:120]}")


# ---- a withheld price must never become an asking price --------------------
# 48A Garnet Road, Westmere is advertised by negotiation on every portal, and
# arrived in the feed carrying $2,350,000 — the hidden SEARCH PRICE agents set so
# a listing appears in buyers' filters. We published it as a valuation, and as a
# 49% margin against a $3.5M CV.
from app.ingest import _detect_listing_type as _lt
from app.pricing.glm import predict as _predict

check("by-negotiation + search price is not 'fixed'",
      _lt("Price by Negotiation", 2_350_000) == "negotiation",
      f"got {_lt('Price by Negotiation', 2_350_000)!r}")
check("a displayed price is still 'fixed'",
      _lt("$829,000 Negotiable", 829_000) == "fixed")
check("'Enquiries over $640,000' is a real number",
      _lt("Enquiries over $640,000", 640_000) == "fixed")
check("a date is not mistaken for a price",
      _lt("Deadline Sale 12 June 2026", 2_350_000) == "tender")

def _p(listing_type):
    return _predict(suburb="Westmere", district="Auckland City", property_type="House",
                    cv=3_500_000, floor=294, land=471, beds=4, baths=2, cars=2, age=30,
                    title=1, method="", pool=False, address="48A Garnet Road",
                    asking_price=2_350_000, listing_type=listing_type)

check("negotiation listing does not take the asking path",
      _p("negotiation").pricing_path != "asking", f"got {_p('negotiation').pricing_path}")
check("and it records why",
      any("no_advertised_price" in w for w in _p("negotiation").warnings))
check("tender listing does not take the asking path",
      _p("tender").pricing_path != "asking")
check("a fixed-price listing still does",
      _p("fixed").pricing_path == "asking")
check("a legacy row with no listing_type still does",
      _p(None).pricing_path == "asking")

# ---- a listing with no advertised price never becomes a deal ---------------
from app.release import _hold_reason as _hr

class _Row:
    """Row-like, matching what release._hold_reason reads."""
    def __init__(self, **kw):
        d = dict(land_area_flag=None, cv_flag=None, floor_area_m2=294,
                 property_type="House", expected_sale_path="asking",
                 asking_price=2_350_000, cv_numeric=3_500_000,
                 valuation_last_sold_value=None, fair_value=3_498_713,
                 margin=0.49, listing_type="fixed")
        d.update(kw); self.__dict__.update(d)

# A no-price listing stays IN the feed — it is the deal signal it must not get,
# and that is suppressed in the pipeline, not here.
for _t in ("negotiation", "auction", "tender", "unknown"):
    check(f"{_t} listing is NOT held from the feed", _hr(_Row(listing_type=_t)) is None,
          f"got {_hr(_Row(listing_type=_t))!r}")
check("a genuine fixed-price deal still publishes",
      _hr(_Row(listing_type="fixed")) is None, f"got {_hr(_Row(listing_type='fixed'))!r}")
check("a row with no listing_type recorded is unaffected",
      _hr(_Row(listing_type=None)) is None)
check("a row-like object missing the field entirely does not raise",
      _hr(type("X", (), {k: v for k, v in _Row().__dict__.items()
                         if k != "listing_type"})()) is None)


# ---- a search price never reaches the database or the screen ---------------
import pandas as _pd
from app.pricing.pipeline import run as _run
from app.pricing.comps import SoldDataset as _SD

_sold = _pd.DataFrame([{"suburb":"Westmere","district":"Auckland City","property_type":"House",
                        "price_numeric":3_400_000+i*10_000,"sale_price":3_400_000+i*10_000,
                        "cv_numeric":3_500_000,"key_floor_area":290,"key_land_area":470,
                        "key_bedrooms":4,"key_bathrooms":2,"days_on_market":25,
                        "sold_date":"5/14/2026","address":f"{i} Sold St"} for i in range(6)])
_base = dict(suburb="Westmere", district="Auckland City", region="Auckland",
             property_type="House", cv_numeric=3_500_000, key_floor_area=294,
             key_land_area=471, key_bedrooms=4, key_bathrooms=2, key_carspaces=2,
             year_built=1995, type_of_title="Freehold", has_swimming_pool=False,
             days_on_market=20)
_fs = _pd.DataFrame([
    {**_base, "address":"48A Garnet Road", "price_display":"Price by Negotiation", "price_numeric":2_350_000},
    {**_base, "address":"50 Garnet Road",  "price_display":"$3,250,000",           "price_numeric":3_250_000},
    {**_base, "address":"52 Garnet Road",  "price_display":"Auction",              "price_numeric":3_500_000},
])
_out = _run(_fs, _SD(_sold), None)
_n = lambda v: None if v is None or (isinstance(v, float) and v != v) else v
_g = _out[_out.address == "48A Garnet Road"].iloc[0]
_f = _out[_out.address == "50 Garnet Road"].iloc[0]
_a = _out[_out.address == "52 Garnet Road"].iloc[0]

check("price_numeric is not duplicated by the merge",
      list(_out.columns).count("price_numeric") == 1)
check("by-negotiation: the search price is cleared",
      _n(_g["price_numeric"]) is None, f"got {_n(_g['price_numeric'])!r}")
check("by-negotiation: no margin", _n(_g["margin"]) is None)
check("by-negotiation: no buy price without a real ask", _n(_g["buy_price"]) is None)
check("by-negotiation: STILL valued", _n(_g["market_value"]) is not None,
      f"got {_n(_g['market_value'])!r}")
check("auction: the CV in the price field is cleared", _n(_a["price_numeric"]) is None)
check("auction: STILL valued", _n(_a["market_value"]) is not None)
check("advertised price is kept", _n(_f["price_numeric"]) == 3_250_000)
check("advertised price still drives the value",
      _n(_f["market_value"]) == round(3_250_000 * 0.95 / 1000) * 1000)
check("advertised price still gets a margin and a buy price",
      _n(_f["margin"]) is not None and _n(_f["buy_price"]) is not None)


# ---- an account whose stored email has capitals can still sign in ----------
# The bug: sign-in lowercased the INPUT and compared it to the stored column as
# written. A row holding "Matt.Grant@Outlook.co.nz" therefore matched nothing —
# not the address as typed (lowered before the compare) and not the lowered form
# (the row still had capitals). Every attempt 401'd with the correct password.
from app.security import hash_password as _hp, _normalise_emails, ensure_seed_admin

_db = SessionLocal()
reset_users(_db)
for _e in ("Matt.Grant@Outlook.co.nz", " jane@example.com ", "bob@example.com"):
    _db.add(M.User(email=_e, password_hash=_hp("secret123"), full_name="T",
                   role="admin", status="approved"))
_db.commit(); _db.close()

def _signin(email, pw="secret123"):
    return client.post("/api/auth/sign-in", data={"username": email, "password": pw}).status_code

check("mixed-case stored email signs in as typed",
      _signin("Matt.Grant@Outlook.co.nz") == 200, f"got {_signin('Matt.Grant@Outlook.co.nz')}")
check("mixed-case stored email signs in lowercased",
      _signin("matt.grant@outlook.co.nz") == 200, f"got {_signin('matt.grant@outlook.co.nz')}")
check("whitespace-padded stored email signs in",
      _signin("jane@example.com") == 200, f"got {_signin('jane@example.com')}")
check("input is trimmed too", _signin("  bob@example.com  ") == 200)
check("wrong password is still refused", _signin("bob@example.com", "nope") == 401)
check("an unknown address is still refused", _signin("nobody@example.com") == 401)

# the boot repair rewrites the rows so future lookups can use the index
_db = SessionLocal()
_normalise_emails(_db)
_stored = sorted((u.email or "") for u in _db.query(M.User).all())
_db.close()
check("boot repair normalises every stored email",
      all(e == e.strip().lower() for e in _stored), f"got {_stored}")
check("boot repair does not drop or merge accounts", len(_stored) == 3, f"got {_stored}")
check("accounts still sign in after the repair", _signin("Matt.Grant@Outlook.co.nz") == 200)

# sign-up must not be able to create a SECOND account for the same person just by
# changing the case — two matching rows make the sign-in lookup ambiguous.
_db = SessionLocal()
reset_users(_db)
_db.add(M.User(email="Taken@example.com", password_hash=_hp("secret123"), full_name="T",
               role="user", status="approved"))
_db.commit(); _db.close()
_r = client.post("/api/auth/sign-up", json={"email": "taken@example.com", "password": "secret123",
                                            "full_name": "Imposter"})
check("sign-up rejects an address that differs only by case",
      _r.status_code == 409, f"got {_r.status_code} {_r.text[:120]}")

# a genuine case-only collision is a merge decision, not a repair: leave it alone
_db = SessionLocal()
reset_users(_db)
for _e in ("Dup@example.com", "dup@example.com"):
    _db.add(M.User(email=_e, password_hash=_hp("secret123"), full_name="T",
                   role="admin", status="approved"))
_db.commit()
_normalise_emails(_db)
_left = sorted((u.email or "") for u in _db.query(M.User).all())
_db.close()
check("a case-only collision is left untouched rather than merged",
      _left == ["Dup@example.com", "dup@example.com"], f"got {_left}")

# restore the seed admin the rest of the suite assumes
_db = SessionLocal(); ensure_seed_admin(_db); _db.close()



# ---- the median price trend, split by bedroom count ------------------------
# A suburb median is a mix. In Remuera a 2-bed unit and a 5-bed villa share a
# postcode and nothing else, so the blended line moves when the MIX moves even
# if neither market did. The chart has to be able to pick one out.
from app.routers.properties import _bed_band, _bed_label, MIN_BED_MONTH_SALES
import random as _r2

_db = SessionLocal()
# Every child of import_batches, not just the sold rows — with foreign keys
# enforced, leaving one behind makes the parent delete fail.
for _t in (M.IngestJob, M.PropertyForSale, M.PropertySold, M.PropertyRent, M.ImportBatch):
    _db.query(_t).delete()
_db.commit()
_b = M.ImportBatch(batch_type="sold", region="Auckland", filename="beds.csv",
                   is_active=True, status="published",
                   published_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
_db.add(_b); _db.commit(); _db.refresh(_b); _bid = _b.id
_rr = _r2.Random(4)
for _m in range(8, 20):                       # 2025-08 .. 2026-07
    _y, _mo = (2025, _m) if _m <= 12 else (2026, _m - 12)
    for _i in range(6):                        # 3-beds, drifting UP
        _db.add(M.PropertySold(import_batch_id=_bid, region="Auckland", suburb="Remuera",
            address=f"{_m}-{_i} A St", beds=3, baths=2, floor_area_m2=150, land_area_m2=500,
            sale_price=1_500_000 + _m * 15_000 + _rr.randint(-60_000, 60_000),
            cv_numeric=1_600_000, days_on_market=35, sold_date=f"{_mo}/12/{_y}"))
    for _i in range(4):                        # 5-beds, FLAT and far dearer
        _db.add(M.PropertySold(import_batch_id=_bid, region="Auckland", suburb="Remuera",
            address=f"{_m}-{_i} B St", beds=5, baths=3, floor_area_m2=300, land_area_m2=900,
            sale_price=3_200_000 + _rr.randint(-120_000, 120_000),
            cv_numeric=3_300_000, days_on_market=50, sold_date=f"{_mo}/12/{_y}"))
    _db.add(M.PropertySold(import_batch_id=_bid, region="Auckland", suburb="Remuera",
        address=f"{_m} C St", beds=2, baths=1, floor_area_m2=80, sale_price=900_000,
        cv_numeric=950_000, days_on_market=30, sold_date=f"{_mo}/12/{_y}"))
_db.commit(); _db.close()

_j = client.get("/api/properties/suburb-stats?suburb=Remuera", headers=H).json()
_bands = {b["beds"]: b for b in _j["by_beds"]}
_drawn = lambda b: [p["median_price"] for p in _bands[b]["points"] if p["median_price"]]

check("the series is split by bedroom count", sorted(_bands) == [2, 3, 5], f"got {sorted(_bands)}")
check("each band is labelled", _bands[3]["label"] == "3 bed" and _bands[5]["label"] == "5 bed")
check("the 3-bed line is drawn across the window", len(_drawn(3)) == 12, f"got {len(_drawn(3))}")
check("the 3-bed line sits near its own level, not the suburb's",
      1_400_000 < _drawn(3)[0] < 1_900_000, f"got {_drawn(3)[0]}")
check("the 5-bed line sits at a completely different level",
      3_000_000 < _drawn(5)[0] < 3_400_000, f"got {_drawn(5)[0]}")
check("the 3-bed market shows the rise that is really in it",
      _drawn(3)[-1] > _drawn(3)[0] + 100_000, f"{_drawn(3)[0]} -> {_drawn(3)[-1]}")
check("the 5-bed market does NOT, because it did not move",
      abs(_drawn(5)[-1] - _drawn(5)[0]) < 250_000, f"{_drawn(5)[0]} -> {_drawn(5)[-1]}")
check("the blended median sits between the two and describes neither",
      _drawn(3)[0] < _j["median_sold"] < _drawn(5)[0]
      and abs(_j["median_sold"] - _drawn(3)[0]) > 100_000
      and abs(_j["median_sold"] - _drawn(5)[0]) > 100_000, f"got {_j['median_sold']}")
check(f"a month with fewer than {MIN_BED_MONTH_SALES} sales in a band is left off the line",
      _bands[2]["sales"] == 12 and _drawn(2) == [], f"got {_drawn(2)}")
check("but the thin band is still reported, with its count",
      _bands[2]["sales"] == 12)
check("every band covers the same months, so the chips line up",
      len({len(b["points"]) for b in _j["by_beds"]}) == 1)

check("7 bedrooms bands into 6+", _bed_band(7) == 6 and _bed_label(6) == "6+ bed")
check("a missing bedroom count is not banded", _bed_band(None) is None and _bed_band(0) is None)


# ---- the suburb picker is a list, built from the data ----------------------
# It used to be a free-text box. A typo, a spelling the feed does not use, and a
# suburb genuinely absent from the batch all produced the same empty panel.
_o = client.get("/api/properties/suburbs", headers=H)
check("GET /suburbs 200", _o.status_code == 200, f"{_o.status_code} {_o.text[:100]}")
_opts = {x["suburb"]: x for x in _o.json()}
check("the picker lists a suburb that has sales", "Remuera" in _opts, f"got {list(_opts)}")
check("and carries its sold count", _opts["Remuera"]["sold"] > 0, f"got {_opts['Remuera']}")
check("names are trimmed", all(k == k.strip() for k in _opts))
check("no blank option is offered", "" not in _opts)
check("the list is sorted, so the dropdown is scannable",
      list(_opts) == sorted(_opts), f"got {list(_opts)}")
check("every listed suburb has something behind it",
      all(v["sold"] or v["live"] for v in _opts.values()))


# ---- admin user management: add, edit, set password, delete ----------------
_db = SessionLocal(); reset_users(_db); _db.close()
_db = SessionLocal(); ensure_seed_admin(_db); _db.close()
_at = client.post("/api/auth/sign-in", data={"username": "a@b.c", "password": "x"}).json()["access_token"]
_AH = {"Authorization": f"Bearer {_at}"}
_meid = client.get("/api/auth/me", headers=_AH).json()["id"]

_r = client.post("/api/admin/users", headers=_AH,
                 json={"email": "bob@example.com", "password": "secret123", "full_name": "Bob"})
check("admin can add a user", _r.status_code == 201, f"{_r.status_code} {_r.text[:90]}")
_bid = _r.json()["id"]
check("the new account can sign in straight away",
      client.post("/api/auth/sign-in", data={"username": "bob@example.com", "password": "secret123"}).status_code == 200)

# resetting a password is the way back in when someone is locked out
check("admin can set a password",
      client.post(f"/api/admin/users/{_bid}/password", headers=_AH,
                  json={"password": "newpass456"}).status_code == 200)
check("the old password stops working",
      client.post("/api/auth/sign-in", data={"username": "bob@example.com", "password": "secret123"}).status_code == 401)
check("the new password works",
      client.post("/api/auth/sign-in", data={"username": "bob@example.com", "password": "newpass456"}).status_code == 200)
check("a too-short password is refused",
      client.post(f"/api/admin/users/{_bid}/password", headers=_AH,
                  json={"password": "short"}).status_code == 422)

# editing details, including the address they sign in with
_r = client.patch(f"/api/admin/users/{_bid}", headers=_AH,
                  json={"email": "Robert@Example.com", "full_name": "Robert", "role": "admin"})
check("admin can edit email, name and role", _r.status_code == 200, f"{_r.status_code} {_r.text[:90]}")
check("the edited email is stored normalised", _r.json()["email"] == "robert@example.com", f"got {_r.json()['email']}")
check("and they sign in on the new address",
      client.post("/api/auth/sign-in", data={"username": "robert@example.com", "password": "newpass456"}).status_code == 200)
check("an edit that omits status leaves it alone", _r.json()["status"] == "approved")
check("an email already in use is refused",
      client.patch(f"/api/admin/users/{_bid}", headers=_AH, json={"email": "a@b.c"}).status_code == 409)

# guards: the ways an admin could lock everyone out
check("you cannot delete the account you are signed in as",
      client.delete(f"/api/admin/users/{_meid}", headers=_AH).status_code == 409)
check("admin can delete another user",
      client.delete(f"/api/admin/users/{_bid}", headers=_AH).status_code == 204)
check("the deleted account can no longer sign in",
      client.post("/api/auth/sign-in", data={"username": "robert@example.com", "password": "newpass456"}).status_code == 401)
check("deleting the last active admin is refused",
      client.delete(f"/api/admin/users/{_meid}", headers=_AH).status_code == 409)
check("demoting the last active admin is refused",
      client.patch(f"/api/admin/users/{_meid}", headers=_AH, json={"role": "user"}).status_code == 409)
check("deactivating the last active admin is refused",
      client.patch(f"/api/admin/users/{_meid}", headers=_AH, json={"status": "deactivated"}).status_code == 409)
check("deleting someone who does not exist is a 404",
      client.delete("/api/admin/users/999999", headers=_AH).status_code == 404)

# a non-admin must not reach any of it
_r = client.post("/api/auth/sign-up", json={"email": "plain@example.com", "password": "secret123"})
_ut = _r.json()["token"]["access_token"]
_UH = {"Authorization": f"Bearer {_ut}"}
check("a normal user cannot list users", client.get("/api/admin/users", headers=_UH).status_code == 403)
check("a normal user cannot delete anyone",
      client.delete(f"/api/admin/users/{_meid}", headers=_UH).status_code == 403)
check("a normal user cannot set a password",
      client.post(f"/api/admin/users/{_meid}/password", headers=_UH,
                  json={"password": "hijacked1"}).status_code == 403)


# ---- the dashboard shows the actual top opportunities ----------------------
# Build a fresh live for-sale batch: earlier blocks in this file retire the ones
# seeded at the top, and headline reads the ACTIVE batch.
_db = SessionLocal()
_db.query(M.PropertyForSale).delete()
for _b0 in _db.query(M.ImportBatch).filter(M.ImportBatch.batch_type == "for_sale").all():
    _b0.is_active = False
_nb = M.ImportBatch(batch_type="for_sale", region="Auckland", filename="top3.csv",
                    is_active=True, status="published",
                    published_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
_db.add(_nb); _db.commit(); _db.refresh(_nb)

# three underpriced listings with different DOLLAR gaps
for _addr, _ask, _fair in (("Small gap", 900_000, 1_000_000),
                           ("Huge gap", 2_600_000, 3_400_000),
                           ("Mid gap", 1_200_000, 1_500_000)):
    _db.add(M.PropertyForSale(
        import_batch_id=_nb.id, region="Auckland", suburb="Remuera", address=_addr,
        # cv deliberately unequal to the asking price: a row where they match is
        # a placeholder listing and _hide_bad_data drops it, correctly.
        asking_price=_ask, cv_numeric=round(_ask * 1.07), fair_value=_fair, floor_area_m2=140,
        land_area_m2=600, is_held=False, is_underpriced=True,
        margin=(_fair - _ask) / _ask, comps_used=10, beds=3, baths=2))
# three subdividable sites: the biggest NET GAIN must lead, not the most lots
for _addr, _lots, _gain in (("Small gain 4 lots", 4, 150_000),
                            ("Big gain 1 lot", 1, 900_000),
                            ("Mid gain 2 lots", 2, 400_000)):
    _db.add(M.PropertyForSale(
        import_batch_id=_nb.id, region="Auckland", suburb="Remuera", address=_addr,
        asking_price=1_000_000, cv_numeric=1_150_000, fair_value=1_200_000,
        floor_area_m2=140, land_area_m2=1200, is_held=False,
        is_subdividable=True, best_net_gain=_gain, max_addl_lots=_lots,
        subdivision_profit=_gain, beds=3, baths=1))
_db.commit(); _db.close()

_h = client.get("/api/dashboards/headline", headers=H)
check("GET /headline 200", _h.status_code == 200, f"{_h.status_code} {_h.text[:90]}")
_j = _h.json()
_tu = _j.get("top_underpriced", [])
check("headline carries the top 3 underpriced", len(_tu) == 3, f"got {len(_tu)}")
check("underpriced are ranked by the DOLLAR gap, not the percentage",
      [d["address"] for d in _tu] == ["Huge gap", "Mid gap", "Small gap"],
      f"got {[d['address'] for d in _tu]}")
check("the headline deal is the same one that leads the list",
      _j["best"]["address"] == _tu[0]["address"])

_ts = _j.get("top_subdividable", [])
check("headline carries the top 3 subdividable", len(_ts) == 3, f"got {len(_ts)}")
check("subdividable are ranked by NET GAIN, not by lot count",
      [d["address"] for d in _ts] == ["Big gain 1 lot", "Mid gain 2 lots", "Small gain 4 lots"],
      f"got {[d['address'] for d in _ts]}")
check("the one-lot site with the biggest gain leads", _ts[0]["max_addl_lots"] == 1, f"got {_ts[0]}")
check("each carries what it is worth", all(d["best_net_gain"] for d in _ts))

# ...but only for an admin. These name the specific houses with the biggest
# margins in the batch — the working output of the model — so a normal user must
# not be able to READ them, not merely fail to see them rendered.
_nr = client.post("/api/auth/sign-up", json={"email": "plain2@example.com", "password": "secret123"})
_NH = {"Authorization": f"Bearer {_nr.json()['token']['access_token']}"}
_db2 = SessionLocal()
_u2 = _db2.get(M.User, _nr.json()["user"]["id"]); _u2.status = "approved"; _db2.commit(); _db2.close()
_nj = client.get("/api/dashboards/headline", headers=_NH)
check("a normal user still gets the headline totals", _nj.status_code == 200, f"got {_nj.status_code}")
check("but no top-3 underpriced", _nj.json()["top_underpriced"] == [], f"got {_nj.json()['top_underpriced']}")
check("and no top-3 subdividable", _nj.json()["top_subdividable"] == [], f"got {_nj.json()['top_subdividable']}")
# The single "biggest gap" hero card is a separate, older feature and stays as
# it was — only the top-3 lists were asked to be admin only. So the check is
# that no address which appears ONLY in those lists reaches a normal user.
check("the withholding is in the API, not just the page",
      "Big gain 1 lot" not in _nj.text and "Mid gain 2 lots" not in _nj.text,
      "subdividable addresses leaked to a non-admin")

# and the deal list can be ordered the same way
_lr = client.get("/api/properties?subdividable=true&order_by=best_net_gain&order_dir=desc", headers=H)
check("the list endpoint accepts order_by=best_net_gain", _lr.status_code == 200,
      f"{_lr.status_code} {_lr.text[:90]}")
_gains = [r.get("best_net_gain") for r in _lr.json()["rows"] if r.get("best_net_gain") is not None]
check("and returns them biggest gain first", _gains == sorted(_gains, reverse=True), f"got {_gains}")



# ---- Ask Ollie: one account key, capped per user per day -------------------
# The assistant used to demand a Claude/OpenAI key from every individual before
# it would answer anything. An admin sets one key; everyone without their own
# uses it, up to a daily allowance.
from app import settings_store as _ss
from app.assistant import keys as _keys, providers as _providers
from app.models import LLM_API_KEY as _K, LLM_DAILY_LIMIT as _L, LLM_PROVIDER as _P

_db = SessionLocal(); reset_users(_db); ensure_seed_admin(_db); _db.close()
_atok = client.post("/api/auth/sign-in", data={"username": "a@b.c", "password": "x"}).json()["access_token"]
_AH = {"Authorization": f"Bearer {_atok}"}

_r = client.get("/api/admin/assistant/key", headers=_AH)
check("GET the account key 200", _r.status_code == 200, f"{_r.status_code} {_r.text[:90]}")
check("it starts unconfigured", _r.json()["configured"] is False)
check("and defaults to 20 questions a day", _r.json()["daily_limit"] == 20, f"got {_r.json()['daily_limit']}")

# A key is verified with a live call before it is stored, so stub the provider.
_real_run = _providers.run
_providers.run = lambda **kw: type("R", (), {"text": "ok", "tools_used": [], "iterations": 1, "queries": []})()
try:
    _r = client.put("/api/admin/assistant/key", headers=_AH,
                    json={"provider": "anthropic", "api_key": "sk-ant-" + "x" * 40, "daily_limit": 3})
    check("an admin can save the account key", _r.status_code == 200, f"{_r.status_code} {_r.text[:120]}")
    check("it reports as connected", _r.json()["configured"] is True)
    check("only the last four characters come back", _r.json()["key_last_four"] == "xxxx"
          and "sk-ant" not in _r.text, f"got {_r.json()}")
    check("the limit was saved with it", _r.json()["daily_limit"] == 3)
finally:
    _providers.run = _real_run

_db = SessionLocal()
check("the key is stored encrypted, never in the clear",
      "sk-ant" not in (_ss.get(_db, _K) or ""), f"got {(_ss.get(_db, _K) or '')[:20]}")
check("and it decrypts back to what was entered",
      _keys.decrypt(_ss.get(_db, _K)) == "sk-ant-" + "x" * 40)
_prov, _plain = _ss.shared_key(_db)
check("the shared key resolves for the assistant", _prov == "anthropic" and _plain)
_db.close()

# a user with no key of their own is put on the account key, and counted
_r = client.post("/api/auth/sign-up", json={"email": "asker@example.com", "password": "secret123"})
_uid = _r.json()["user"]["id"]
_db = SessionLocal()
_u = _db.get(M.User, _uid); _u.status = "approved"; _db.commit()
_q = _ss.quota_for(_db, _u)
check("a user without their own key is on the shared one", _q["shared"] is True and _q["configured"] is True)
check("with the full allowance to start", _q["used"] == 0 and _q["remaining"] == 3, f"got {_q}")

# each answered question spends one
for _i in range(3):
    _db.add(M.AssistantLog(user_id=_uid, question=f"q{_i}", answer="a", ok=True))
_db.commit()
check("answers spend the allowance", _ss.quota_for(_db, _u)["remaining"] == 0,
      f"got {_ss.quota_for(_db, _u)}")

# a failed question must not
_db.add(M.AssistantLog(user_id=_uid, question="broke", answer=None, ok=False))
_db.commit()
check("a failed question does not spend it", _ss.used_today(_db, _uid) == 3,
      f"got {_ss.used_today(_db, _uid)}")

# their own key: unlimited, and it takes precedence
_u.llm_provider = "openai"; _u.llm_api_key_encrypted = _keys.encrypt("sk-" + "y" * 40)
_db.commit()
_q = _ss.quota_for(_db, _u)
check("a user with their own key is not capped", _q["shared"] is False and _q["limit"] is None, f"got {_q}")
_db.close()

from app.assistant.agent import ask as _ask, AssistantUnavailable as _AU
_seen = {}
_providers.run = lambda **kw: (_seen.update(kw) or
                               type("R", (), {"text": "ok", "tools_used": [], "iterations": 1, "queries": []})())
try:
    _db = SessionLocal(); _u = _db.get(M.User, _uid)
    _ask(_u, "hi", [], shared=("anthropic", "SHARED"))
    check("their own key wins over the account key", _seen["api_key"].startswith("sk-y"),
          f"got {_seen['api_key'][:8]}")
    _u.llm_provider = None; _u.llm_api_key_encrypted = None; _db.commit()
    _seen.clear()
    _ask(_u, "hi", [], shared=("anthropic", "SHARED"))
    check("without one, the account key is used", _seen["api_key"] == "SHARED", f"got {_seen}")
    _db.close()
finally:
    _providers.run = _real_run

# with neither, the message names both ways out rather than only "Settings"
_db = SessionLocal(); _u = _db.get(M.User, _uid)
try:
    _ask(_u, "hi", [], shared=(None, None))
    check("no key anywhere raises", False, "did not raise")
except _AU as _e:
    check("no key anywhere names both ways to fix it",
          "admin" in str(_e).lower() and "settings" in str(_e).lower(), f"got {_e}")
_db.close()

# the limit can be changed without re-entering the key, and 0 switches it off
_r = client.put("/api/admin/assistant/limit", headers=_AH, json={"daily_limit": 0})
check("the limit can be changed on its own", _r.status_code == 200 and _r.json()["daily_limit"] == 0,
      f"{_r.status_code} {_r.text[:90]}")
check("and the key survives that", _r.json()["configured"] is True)

# only admins may touch any of it
_utok = client.post("/api/auth/sign-in", data={"username": "asker@example.com", "password": "secret123"}).json()["access_token"]
_UH2 = {"Authorization": f"Bearer {_utok}"}
check("a normal user cannot read the account key",
      client.get("/api/admin/assistant/key", headers=_UH2).status_code == 403)
check("a normal user cannot set it",
      client.put("/api/admin/assistant/key", headers=_UH2,
                 json={"provider": "anthropic", "api_key": "sk-ant-" + "z" * 40}).status_code == 403)
check("a normal user cannot raise their own limit",
      client.put("/api/admin/assistant/limit", headers=_UH2, json={"daily_limit": 999}).status_code == 403)

# removing the key leaves nothing behind
check("an admin can remove it",
      client.delete("/api/admin/assistant/key", headers=_AH).json()["configured"] is False)
_db = SessionLocal()
check("and the stored value is gone", not _ss.get(_db, _K))
_db.close()


# ---- the build number, so a bug report can be checked against a build ------
from app.version import VERSION as _V, BUILT_AT as _BA

_r = client.get("/api/version")
check("GET /api/version 200", _r.status_code == 200, f"{_r.status_code} {_r.text[:90]}")
check("it reports the build number", _r.json().get("version") == _V, f"got {_r.json()}")
check("and when it was cut", _r.json().get("built_at") == _BA)
check("the version endpoint needs no login — the times you need it include "
      "the times nobody can sign in",
      client.get("/api/version").status_code == 200)
check("/health carries it too", client.get("/health").json().get("version") == _V)
check("the version looks like N.N so the dashboard can compare the two halves",
      len(_V.split(".")) == 2 and all(p.isdigit() for p in _V.split(".")), f"got {_V!r}")


# ---- max_addl_lots means ADDITIONAL lots on every path ---------------------
# The validation report has shown "+52.57% bias" on this output for every run.
# The THAB terrace path returned the TOTAL terrace count where every other path
# returns sections - 1, so each THAB row was over by exactly one — which on a
# two-lot site is a 100% overstatement.
from app.pricing.subdivision import compute as _sub

_thab = _sub(zone="Residential - Terrace Housing and Apartment Building Zone",
             land_area=1400.0, buy_price=1_500_000, section_rate=1500,
             rate_source="section_rate", address="1 Terrace Way", property_type="House",
             title_type="Freehold", improvement_value=300_000, cv=1_600_000,
             beds=3, baths=1)
check("a THAB site yields more than one dwelling", (_thab.sections or 0) >= 2,
      f"got sections={_thab.sections}")
check("additional lots is one FEWER than the dwellings built",
      _thab.max_addl_lots == (_thab.sections or 0) - 1,
      f"sections={_thab.sections} addl={_thab.max_addl_lots}")

_split = _sub(zone="Residential - Mixed Housing Urban Zone", land_area=1400.0,
              buy_price=1_200_000, section_rate=1200, rate_source="section_rate",
              address="2 Split St", property_type="House", title_type="Freehold",
              improvement_value=300_000, cv=1_500_000, beds=3, baths=1)
check("the bare-section path already meant additional, and still does",
      _split.max_addl_lots == (_split.sections or 0) - 1,
      f"sections={_split.sections} addl={_split.max_addl_lots}")
check("both paths agree on what the field means",
      _thab.max_addl_lots is not None and _split.max_addl_lots is not None)
check("a site that cannot be split reports no lot count",
      _sub(zone="Residential - Single House Zone", land_area=400.0, buy_price=900_000,
           section_rate=1200, rate_source="section_rate", address="3 Small Pl",
           property_type="House", title_type="Freehold", improvement_value=200_000,
           cv=1_000_000, beds=3, baths=1).max_addl_lots is None)


# ---- the admin panel must not answer 500 when a table is missing ----------
# Production returned 500 on /api/admin/assistant/key, /usage and
# /api/admin/users/{id}. A 500 in a browser console carries nothing, and the
# person reading it cannot see the server log — so each round cost a deploy
# cycle to guess at. These endpoints now either recover or say what is wrong.
from sqlalchemy import text as _text, inspect as _inspect
from app.db import engine as _eng
from app import settings_store as _ss2

_db = SessionLocal(); reset_users(_db); ensure_seed_admin(_db); _db.close()
_tk = client.post("/api/auth/sign-in", data={"username": "a@b.c", "password": "x"}).json()["access_token"]
_AH2 = {"Authorization": f"Bearer {_tk}"}

with _eng.begin() as _c:
    _c.execute(_text("DROP TABLE IF EXISTS app_settings"))
check("app_settings really is gone", "app_settings" not in _inspect(_eng).get_table_names())

check("GET the key does not 500 with the table missing",
      client.get("/api/admin/assistant/key", headers=_AH2).status_code == 200,
      f"got {client.get('/api/admin/assistant/key', headers=_AH2).status_code}")
check("usage does not 500 either",
      client.get("/api/admin/assistant/usage", headers=_AH2).status_code == 200)
_r = client.put("/api/admin/assistant/limit", headers=_AH2, json={"daily_limit": 20})
check("saving a setting recreates the table rather than failing",
      _r.status_code == 200, f"{_r.status_code} {_r.text[:120]}")
check("and the table is back", "app_settings" in _inspect(_eng).get_table_names())

# deleting a user must survive a missing dependent table
with _eng.begin() as _c:
    _c.execute(_text("DROP TABLE IF EXISTS app_settings"))
_r = client.post("/api/admin/users", headers=_AH2,
                 json={"email": "doomed@example.com", "password": "secret123"})
_did = _r.json()["id"]
_r = client.delete(f"/api/admin/users/{_did}", headers=_AH2)
check("deleting a user survives a missing dependent table",
      _r.status_code == 204, f"{_r.status_code} {_r.text[:120]}")
check("and the account really is gone",
      client.post("/api/auth/sign-in",
                  data={"username": "doomed@example.com", "password": "secret123"}).status_code == 401)

# the diagnostic that ends the guessing
_r = client.get("/api/admin/diagnostics", headers=_AH2)
check("GET /api/admin/diagnostics 200", _r.status_code == 200, f"{_r.status_code} {_r.text[:90]}")
_d = _r.json()
check("it names the build", _d.get("version") and _d.get("built_at"), f"got {_d}")
check("it names the database engine", _d.get("dialect") in ("sqlite", "postgresql"), f"got {_d.get('dialect')}")
check("it lists the tables the models expect but the database lacks",
      "app_settings" in _d.get("tables_missing", []), f"got {_d.get('tables_missing')}")
check("it reports the real error per admin feature, not just present/absent",
      "app_settings" in _d["checks"] and "Error" in _d["checks"]["app_settings"],
      f"got {_d['checks']}")
check("it is admin only", client.get("/api/admin/diagnostics").status_code == 401)

_ss2.ensure_table()   # leave the schema whole for anything after this


# ---- creating a user and setting a password must never be a bare 500 -------
from app.security import hashing_selftest as _hs, PasswordHashingUnavailable as _PHU
import app.security as _S

_db = SessionLocal(); reset_users(_db); ensure_seed_admin(_db); _db.close()
_t2 = client.post("/api/auth/sign-in", data={"username": "a@b.c", "password": "x"}).json()["access_token"]
_H3 = {"Authorization": f"Bearer {_t2}"}

check("this server can hash a password", _hs()["hash"].startswith("ok"), f"got {_hs()}")
check("and verify the hash it just made", _hs()["verify"] == "ok", f"got {_hs()}")
check("diagnostics reports the hashing self-test",
      "password_hashing" in client.get("/api/admin/diagnostics", headers=_H3).json())

# with hashing genuinely broken, the answer must NAME it, not return 500
_saved_ctx, _saved_bc = _S.pwd_context, _S._bcrypt
_S.pwd_context = type("X", (), {"hash": staticmethod(
    lambda p: (_ for _ in ()).throw(ValueError("bcrypt backend broken")))})()
_S._bcrypt = None
try:
    _r = client.post("/api/admin/users", headers=_H3,
                     json={"email": "cant@example.com", "password": "secret123"})
    check("creating a user with hashing broken is 503, not 500", _r.status_code == 503,
          f"got {_r.status_code}")
    check("and the message names the library and the error",
          "bcrypt" in str(_r.json()["detail"]).lower(), f"got {_r.json()['detail']}")
    _r = client.post("/api/auth/sign-up", json={"email": "x9@example.com", "password": "secret123"})
    check("sign-up says the same rather than 500ing", _r.status_code == 503, f"got {_r.status_code}")
    check("the diagnostic reports it too",
          "ValueError" in str(_hs()["hash"]), f"got {_hs()}")
finally:
    _S.pwd_context, _S._bcrypt = _saved_ctx, _saved_bc

check("hashing works again once restored", _hs()["hash"].startswith("ok"))

# a boot-time repair must never be able to stop the server starting
_db = SessionLocal()
_S.pwd_context = type("X", (), {"hash": staticmethod(
    lambda p: (_ for _ in ()).throw(ValueError("boom")))})()
_S._bcrypt = None
try:
    ensure_seed_admin(_db)
    check("ensure_seed_admin swallows a failure instead of aborting startup", True)
except Exception as _e:
    check("ensure_seed_admin swallows a failure instead of aborting startup", False, f"raised {_e}")
finally:
    _S.pwd_context, _S._bcrypt = _saved_ctx, _saved_bc
    _db.close()

# the lookup must not depend on one dialect's trim()
_db = SessionLocal(); reset_users(_db)
_db.add(M.User(email="  Spaced@Example.com  ", password_hash=_hp("secret123"),
               role="admin", status="approved"))
_db.commit()
from app.security import find_user_by_email as _fube
check("a padded, mixed-case address is still found without trim()",
      (_fube(_db, "spaced@example.com") or None) is not None)
check("and a genuinely absent one is still absent", _fube(_db, "nobody@example.com") is None)
_db.close()
_db = SessionLocal(); reset_users(_db); ensure_seed_admin(_db); _db.close()


# ---- the bug log ----------------------------------------------------------
# A report written in prose loses the two facts that decide how long a fault
# takes to fix: which build it happened on, and what the server actually said.
_db = SessionLocal(); reset_users(_db); ensure_seed_admin(_db); _db.close()
_bt = client.post("/api/auth/sign-in", data={"username": "a@b.c", "password": "x"}).json()["access_token"]
_BH = {"Authorization": f"Bearer {_bt}"}

_r = client.post("/api/bugs", headers=_BH, json={
    "title": "Cannot create users", "detail": "Clicked Create, nothing happened",
    "page": "/admin/users", "severity": "blocker", "app_version": "9.9",
    "api_errors": [{"at": "2026-08-15T08:00:00Z", "path": "/api/admin/users",
                    "status": 500, "detail": "Internal Server Error"}]})
check("a bug can be filed", _r.status_code == 201, f"{_r.status_code} {_r.text[:120]}")
_bug = _r.json()
_bid = _bug["id"]
check("the API stamps its OWN version, not the browser's claim",
      _bug["api_version"] == _V and _bug["app_version"] == "9.9", f"got {_bug}")
check("the failing request is captured with the server's message",
      _bug["api_errors"][0]["status"] == 500
      and _bug["api_errors"][0]["path"] == "/api/admin/users", f"got {_bug['api_errors']}")
check("who reported it is recorded", _bug["reported_by_email"] == "a@b.c")
check("it starts open", _bug["status"] == "open")
check("a nonsense severity is refused",
      client.post("/api/bugs", headers=_BH,
                  json={"title": "x y z", "severity": "catastrophic"}).status_code == 400)

# Count only what was FILED — 5xx raised elsewhere in this suite now file
# themselves too, which is the point, but makes a bare total meaningless.
_manual = [b for b in client.get("/api/admin/bugs", headers=_BH).json() if b["source"] == "manual"]
check("admin can list the log", len(_manual) == 1, f"got {len(_manual)}")
check("and filter by status",
      len(client.get("/api/admin/bugs?status=fixed", headers=_BH).json()) == 0)
_r = client.patch(f"/api/admin/bugs/{_bid}", headers=_BH,
                  json={"status": "fixed", "resolution": "bcrypt could not import"})
check("marking it fixed records when", _r.json()["status"] == "fixed" and _r.json()["resolved_at"],
      f"got {_r.json()}")
check("and what was found", _r.json()["resolution"] == "bcrypt could not import")
check("reopening clears the resolved stamp",
      client.patch(f"/api/admin/bugs/{_bid}", headers=_BH,
                   json={"status": "open"}).json()["resolved_at"] is None)

_csv = client.get("/api/admin/bugs/export.csv", headers=_BH)
check("the log exports as CSV", _csv.status_code == 200, f"got {_csv.status_code}")
check("with a header row naming the captured context",
      "app_version" in _csv.text.splitlines()[0] and "api_errors" in _csv.text.splitlines()[0],
      f"got {_csv.text.splitlines()[0][:120]}")
check("and the server's error flattened into a readable column",
      "500 /api/admin/users" in _csv.text, f"got {_csv.text[:200]}")
check("the CSV downloads as a file rather than opening in the tab",
      "attachment" in _csv.headers.get("content-disposition", ""))

# filing is open to any signed-in user; reading the log is not
_r = client.post("/api/auth/sign-up", json={"email": "reporter@example.com", "password": "secret123"})
_RH = {"Authorization": f"Bearer {_r.json()['token']['access_token']}"}
check("any signed-in user can report a bug — the person who hits it rarely has "
      "the admin password",
      client.post("/api/bugs", headers=_RH,
                  json={"title": "Map will not load"}).status_code == 201)
check("but cannot read the log", client.get("/api/admin/bugs", headers=_RH).status_code == 403)
check("nor export it", client.get("/api/admin/bugs/export.csv", headers=_RH).status_code == 403)
check("nor delete anything", client.delete(f"/api/admin/bugs/{_bid}", headers=_RH).status_code == 403)
check("filing anonymously is refused", client.post("/api/bugs", json={"title": "drive by"}).status_code == 401)

check("admin can delete a report", client.delete(f"/api/admin/bugs/{_bid}", headers=_BH).status_code == 204)
check("deleting one that is gone is a 404",
      client.delete(f"/api/admin/bugs/{_bid}", headers=_BH).status_code == 404)


# ---- bugs that file themselves --------------------------------------------
# The log only held what someone thought to report. A 500 lived in a server log
# nobody was reading, and a crash in the page lived in a console nobody had open.
from fastapi import APIRouter as _AR
from app.routers import bugs as _bugsmod

_db = SessionLocal()
_db.query(M.BugReport).delete(); _db.commit(); _db.close()

_boom = _AR()
@_boom.get("/api/_selftest_boom")
def _selftest_boom():
    raise RuntimeError("kaboom")
app.include_router(_boom)

from fastapi.testclient import TestClient as _TC
with _TC(app, raise_server_exceptions=False) as _sc:
    _st = _sc.post("/api/auth/sign-in", data={"username": "a@b.c", "password": "x"}).json()["access_token"]
    _SH = {"Authorization": f"Bearer {_st}"}
    for _ in range(3):
        _r = _sc.get("/api/_selftest_boom")
    check("an unhandled error still answers a plain 500", _r.status_code == 500,
          f"got {_r.status_code}")
    check("and does not leak the traceback to the caller",
          "kaboom" not in _r.text, f"got {_r.text[:120]}")

    _auto = _sc.get("/api/admin/bugs", headers=_SH).json()
    check("the server error filed itself", len(_auto) == 1, f"got {len(_auto)}")
    _a = _auto[0]
    check("named by exception and endpoint",
          "RuntimeError" in _a["title"] and "/api/_selftest_boom" in _a["title"], f"got {_a['title']}")
    check("marked as filed automatically", _a["source"] == "server", f"got {_a['source']}")
    check("three identical failures are ONE entry, counted",
          _a["occurrences"] == 3, f"got {_a['occurrences']}")
    check("the traceback is kept server-side on the report",
          "kaboom" in (_a["detail"] or ""))
    check("treated as a blocker", _a["severity"] == "blocker")

    # a crash in the page
    _r = _sc.post("/api/bugs/client", headers=_SH, json={
        "message": "The width(-1) and height(-1) of chart should be greater than 0",
        "stack": "at Chart.render (354-90259a.js:1)", "page": "/today", "app_version": "9.9"})
    check("a browser crash is accepted", _r.status_code == 202, f"got {_r.status_code}")
    _rows = _sc.get("/api/admin/bugs", headers=_SH).json()
    _b = [x for x in _rows if x["source"] == "browser"][0]
    check("filed as a browser fault", _b["title"].startswith("[browser]"), f"got {_b['title']}")
    check("with the build the PAGE was on", _b["app_version"] == "9.9")
    check("and the build the API was on, from the server",
          _b["api_version"] == _V, f"got {_b['api_version']}")

    # the same crash from a later, differently-minified build is still one fault
    _sc.post("/api/bugs/client", headers=_SH, json={
        "message": "The width(-1) and height(-1) of chart should be greater than 0",
        "stack": "at Chart.render (DIFFERENT-hash.js:9)", "page": "/today", "app_version": "9.9"})
    _b2 = [x for x in _sc.get("/api/admin/bugs", headers=_SH).json() if x["source"] == "browser"]
    check("a rebuilt bundle does not re-file the same crash",
          len(_b2) == 1 and _b2[0]["occurrences"] == 2, f"got {[(x['occurrences']) for x in _b2]}")

    # a different page IS a different fault
    _sc.post("/api/bugs/client", headers=_SH, json={
        "message": "The width(-1) and height(-1) of chart should be greater than 0",
        "page": "/trends", "app_version": "9.9"})
    check("the same message on another page is its own entry",
          len([x for x in _sc.get("/api/admin/bugs", headers=_SH).json() if x["source"] == "browser"]) == 2)

    # closing it means a later occurrence opens a fresh one rather than
    # silently reviving something already reviewed
    _sc.patch(f"/api/admin/bugs/{_a['id']}", headers=_SH, json={"status": "fixed"})
    _sc.get("/api/_selftest_boom")
    _still = [x for x in _sc.get("/api/admin/bugs?status=open", headers=_SH).json()
              if x["source"] == "server"]
    check("a fault that recurs after being closed is filed again, not resurrected",
          len(_still) == 1 and _still[0]["id"] != _a["id"], f"got {[(x['id']) for x in _still]}")

    _csv = _sc.get("/api/admin/bugs/export.csv", headers=_SH)
    check("the CSV carries the source and the count",
          "source" in _csv.text.splitlines()[0] and "occurrences" in _csv.text.splitlines()[0])

    check("reporting a crash anonymously is refused",
          _sc.post("/api/bugs/client", json={"message": "drive by"}).status_code == 401)

_db = SessionLocal(); _db.query(M.BugReport).delete(); _db.commit(); _db.close()


# ---- the deliberate failures get logged too --------------------------------
# The unhandled-exception handler only sees CRASHES. Every error this codebase
# raises on purpose — "settings unavailable: UndefinedTable", "could not delete:
# FOREIGN KEY constraint failed" — is an HTTPException, which FastAPI handles, so
# none of them reached the log. Those are the most useful ones: someone already
# worked out what went wrong and wrote it down.
from fastapi import APIRouter as _AR2, HTTPException as _HE

_db = SessionLocal(); _db.query(M.BugReport).delete(); _db.commit(); _db.close()

_deliberate = _AR2()
@_deliberate.get("/api/_selftest_503")
def _s503():
    raise _HE(status_code=503, detail="Assistant settings are unavailable: UndefinedTable")
@_deliberate.get("/api/_selftest_404")
def _s404():
    raise _HE(status_code=404, detail="Not found")
@_deliberate.get("/api/_selftest_422")
def _s422():
    raise _HE(status_code=422, detail="Password must be at least 8 characters")
app.include_router(_deliberate)

with _TC(app, raise_server_exceptions=False) as _dc:
    _dt = _dc.post("/api/auth/sign-in", data={"username": "a@b.c", "password": "x"}).json()["access_token"]
    _DH = {"Authorization": f"Bearer {_dt}"}

    _r = _dc.get("/api/_selftest_503")
    check("a deliberate 503 still answers 503", _r.status_code == 503)
    check("and still carries its message to the caller",
          "UndefinedTable" in _r.json()["detail"], f"got {_r.json()}")
    _rows = _dc.get("/api/admin/bugs", headers=_DH).json()
    check("the 503 was logged", len(_rows) == 1, f"got {len(_rows)}")
    check("with the reason someone already wrote down",
          "UndefinedTable" in (_rows[0]["detail"] or ""), f"got {_rows[0]['detail']}")

    # the application working is not a fault
    _dc.get("/api/_selftest_404")
    _dc.get("/api/_selftest_422")
    _dc.get("/api/admin/bugs/999999", headers=_DH)
    check("a 404 is not filed as a bug",
          len(_dc.get("/api/admin/bugs", headers=_DH).json()) == 1,
          "a stale link is the app working, not a fault")
    check("nor a 422 on a mistyped form",
          not any("422" in r["title"] for r in _dc.get("/api/admin/bugs", headers=_DH).json()))
    check("nor a 401 on an expired token",
          _dc.get("/api/admin/bugs").status_code == 401
          and len(_dc.get("/api/admin/bugs", headers=_DH).json()) == 1)

    # repeats still collapse
    for _ in range(4):
        _dc.get("/api/_selftest_503")
    _rows = _dc.get("/api/admin/bugs", headers=_DH).json()
    check("repeats of a deliberate failure collapse onto one entry",
          len(_rows) == 1 and _rows[0]["occurrences"] == 5,
          f"got {len(_rows)} rows, {_rows[0]['occurrences']} occurrences")

_db = SessionLocal(); _db.query(M.BugReport).delete(); _db.commit(); _db.close()


# ---- the app builds its own schema on startup ------------------------------
# Production's boot log has no trace of db_bootstrap, so the start command in use
# is not the Procfile's, and every table added after the original schema was
# simply missing: assistant_logs, app_settings, the geo tables, bug_reports.
# That one fact was diagnosed three separate times as three different bugs.
from sqlalchemy import inspect as _insp, text as _txt
from app.db import engine as _eng2

_LATER_TABLES = ("assistant_logs", "app_settings", "bug_reports",
                 "parcel_cache", "building_overrides")
with _eng2.begin() as _c:
    for _t in _LATER_TABLES:
        _c.execute(_txt(f"DROP TABLE IF EXISTS {_t}"))
_gone = set(_insp(_eng2).get_table_names())
check("the newer tables really are gone",
      all(t not in _gone for t in _LATER_TABLES), f"got {sorted(_gone)}")

with _TC(app) as _boot:            # entering the client runs the app's lifespan
    _have = set(_insp(_eng2).get_table_names())
    for _t in _LATER_TABLES:
        check(f"startup created {_t}", _t in _have, "still missing after boot")
    _bt2 = _boot.post("/api/auth/sign-in", data={"username": "a@b.c", "password": "x"}).json()["access_token"]
    _BH2 = {"Authorization": f"Bearer {_bt2}"}
    check("assistant usage works instead of reporting an undefined table",
          _boot.get("/api/admin/assistant/usage", headers=_BH2).status_code == 200)
    check("the bug log works", _boot.get("/api/admin/bugs", headers=_BH2).status_code == 200)
    check("the assistant key can be read",
          _boot.get("/api/admin/assistant/key", headers=_BH2).status_code == 200)


# ---- picking a suburb must actually filter --------------------------------
# On the all-properties page, choosing a district zoomed the map and choosing a
# suburb did nothing. The dropdown is built from TRIMMED names — it has to be,
# or the same suburb appears three times — but the filter matched the column
# exactly, and the scraped values are not clean. "Remuera" never equals
# "Remuera ", so it returned no rows; with no points the map has nothing to fit,
# and a filter that returns nothing looks exactly like a control that is dead.
_db = SessionLocal()
_db.query(M.PropertyForSale).delete()
for _b0 in _db.query(M.ImportBatch).filter(M.ImportBatch.batch_type == "for_sale").all():
    _b0.is_active = False
_ab = M.ImportBatch(batch_type="for_sale", region="Auckland", filename="areas.csv",
                    is_active=True, status="published",
                    published_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
_db.add(_ab); _db.commit(); _db.refresh(_ab)
for _i, _sub in enumerate(("Remuera ", " Remuera", "REMUERA", "Remuera", "Mount Eden")):
    _db.add(M.PropertyForSale(
        import_batch_id=_ab.id, region="Auckland", suburb=_sub, district=" Auckland City ",
        address=f"{_i} Area St", asking_price=1_000_000, cv_numeric=1_200_000,
        fair_value=1_300_000, floor_area_m2=140, land_area_m2=600, is_held=False,
        latitude=-36.87, longitude=174.79, beds=3, baths=1))
_db.commit(); _db.close()

_opts = client.get("/api/properties/suburbs", headers=H).json()
_names = [o["suburb"] for o in _opts]
check("four spellings of one suburb are offered once",
      _names.count("Remuera") + _names.count("REMUERA") == 1, f"got {_names}")
check("and the count covers every spelling",
      next(o["live"] for o in _opts if o["suburb"].lower() == "remuera") == 4,
      f"got {_opts}")

for _n in _names:
    _m = client.get(f"/api/properties/map?suburb={_n}", headers=H).json()
    check(f"picking {_n!r} returns points for the map", _m["count"] > 0,
          "an empty result leaves the map where it was, which reads as a dead control")
    _l = client.get(f"/api/properties?suburb={_n}", headers=H).json()
    check(f"and the list agrees for {_n!r}", _l["total"] == _m["count"],
          f"map {_m['count']} vs list {_l['total']}")

check("a padded district still matches too",
      client.get("/api/properties/map?district=Auckland City", headers=H).json()["count"] == 5)
check("a suburb with nothing in it returns nothing, not everything",
      client.get("/api/properties/map?suburb=Nowhere", headers=H).json()["count"] == 0)
check("an empty suburb value is not treated as a filter",
      client.get("/api/properties/map?suburb=", headers=H).json()["count"] == 5)


# ---- one rule for matching an area, everywhere -----------------------------
# The trends panel worked while the properties filter did not, because they
# matched differently: ilike (case-insensitive) versus == (not). Two rules for
# one concept is why only one of them was broken, and why it stayed hidden.
_db = SessionLocal()
for _b0 in _db.query(M.ImportBatch).filter(M.ImportBatch.batch_type == "sold").all():
    _b0.is_active = False
_sb = M.ImportBatch(batch_type="sold", region="Auckland", filename="areas-sold.csv",
                    is_active=True, status="published",
                    published_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
_db.add(_sb); _db.commit(); _db.refresh(_sb)
for _i, _sub in enumerate(("Remuera ", "REMUERA", "Remuera", " Remuera")):
    _db.add(M.PropertySold(
        import_batch_id=_sb.id, region="Auckland", suburb=_sub, district="Auckland City",
        address=f"{_i} Sold Ave", sale_price=2_000_000 + _i * 50_000, cv_numeric=1_900_000,
        sale_method="auction" if _i % 2 else "negotiation", floor_area_m2=200,
        land_area_m2=600, beds=4, baths=2, days_on_market=30,
        sold_date=datetime(2026, 7, 15).date()))
_db.commit(); _db.close()

for _q in ("Remuera", "remuera", "REMUERA"):
    _st = client.get(f"/api/properties/suburb-stats?suburb={_q}", headers=H).json()
    check(f"suburb-stats finds every spelling for {_q!r}", _st["sold_count"] == 4,
          f"got {_st['sold_count']}")
    check(f"and the sale-method panel is populated for {_q!r}",
          len(_st.get("by_method", [])) >= 1, f"got {_st.get('by_method')}")

_sl = client.get("/api/sold?suburb=Remuera", headers=H)
check("the sold list matches every spelling too",
      _sl.status_code == 200 and _sl.json()["total"] == 4, f"got {_sl.json().get('total')}")
check("a suburb with no sales returns none, not all",
      client.get("/api/sold?suburb=Nowhere", headers=H).json()["total"] == 0)


# ---- the picker must only offer what the screen can show -------------------
# Why suburb trends worked and all-properties did not, even after the matching
# fix: the list merged sold and live. The sold archive covers far more suburbs
# than any single week of listings, so the properties page was offering suburbs
# with nothing live in them. Pick one, get a blank screen — which looks exactly
# like a filter that does nothing. Trends worked because it reads sold data, and
# every option had sales behind it.
_db = SessionLocal()
_db.query(M.PropertyForSale).delete(); _db.query(M.PropertySold).delete()
for _b0 in _db.query(M.ImportBatch).all():
    _b0.is_active = False
_fb2 = M.ImportBatch(batch_type="for_sale", region="Auckland", filename="live.csv",
                     is_active=True, status="published",
                     published_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
_sb2 = M.ImportBatch(batch_type="sold", region="Auckland", filename="sold.csv",
                     is_active=True, status="published",
                     published_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
_db.add_all([_fb2, _sb2]); _db.commit(); _db.refresh(_fb2); _db.refresh(_sb2)
_db.add(M.PropertyForSale(
    import_batch_id=_fb2.id, region="Auckland", suburb="Remuera", district="Auckland City",
    address="1 Live St", asking_price=1_000_000, cv_numeric=1_200_000, fair_value=1_300_000,
    floor_area_m2=140, land_area_m2=600, is_held=False, latitude=-36.87, longitude=174.79,
    beds=3, baths=1))
for _sub in ("Remuera", "Papakura", "Henderson"):
    _db.add(M.PropertySold(
        import_batch_id=_sb2.id, region="Auckland", suburb=_sub, district="Auckland City",
        address=f"1 {_sub} Rd", sale_price=1_500_000, cv_numeric=1_400_000,
        floor_area_m2=150, land_area_m2=500, beds=3, baths=1,
        sold_date=datetime(2026, 7, 15).date()))
_db.commit(); _db.close()

_live = [o["suburb"] for o in client.get("/api/properties/suburbs?dataset=for_sale", headers=H).json()]
_any = [o["suburb"] for o in client.get("/api/properties/suburbs?dataset=any", headers=H).json()]
_sold = [o["suburb"] for o in client.get("/api/properties/suburbs?dataset=sold", headers=H).json()]
check("the properties picker offers only suburbs with live listings",
      _live == ["Remuera"], f"got {_live}")
check("the trends picker still offers every suburb with sales",
      sorted(_sold) == ["Henderson", "Papakura", "Remuera"], f"got {_sold}")
check("the merged list is still available for anything that wants it",
      sorted(_any) == ["Henderson", "Papakura", "Remuera"], f"got {_any}")

# the point of it: EVERY option on the properties picker returns something
for _n in _live:
    _c = client.get(f"/api/properties/map?suburb={_n}", headers=H).json()["count"]
    check(f"picking {_n!r} on the properties page shows listings", _c > 0,
          "a picker option that returns nothing is indistinguishable from a broken filter")
check("a sold-only suburb is no longer offered there", "Papakura" not in _live)


# ---- the two feeds may not spell a suburb the same way ---------------------
# The last shape that fits "select a suburb and NOTHING happens, for any suburb,
# while district is fine": the picker is built from one feed's vocabulary and the
# filter runs against the other's. A sold archive saying "Remuera" against
# listings saying "Remuera, Auckland" is one suburb written two ways, and an
# exact comparison calls them different places — so every option matches nothing.
# District survives because its vocabulary is small and shared.
_db = SessionLocal()
_db.query(M.PropertyForSale).delete(); _db.query(M.PropertySold).delete()
for _b0 in _db.query(M.ImportBatch).all():
    _b0.is_active = False
_fb3 = M.ImportBatch(batch_type="for_sale", region="Auckland", filename="live2.csv",
                     is_active=True, status="published",
                     published_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
_sb3 = M.ImportBatch(batch_type="sold", region="Auckland", filename="sold2.csv",
                     is_active=True, status="published",
                     published_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
_db.add_all([_fb3, _sb3]); _db.commit(); _db.refresh(_fb3); _db.refresh(_sb3)
for _sub in ("Remuera, Auckland", "Mount Eden, Auckland", "Mount Albert, Auckland"):
    _db.add(M.PropertyForSale(
        import_batch_id=_fb3.id, region="Auckland", suburb=_sub, district="Auckland City",
        address=f"1 {_sub}", asking_price=1_000_000, cv_numeric=1_200_000,
        fair_value=1_300_000, floor_area_m2=140, land_area_m2=600, is_held=False,
        latitude=-36.87, longitude=174.79, beds=3, baths=1))
for _sub in ("Remuera", "Mount Eden", "Papakura"):
    _db.add(M.PropertySold(
        import_batch_id=_sb3.id, region="Auckland", suburb=_sub, district="Auckland City",
        address=f"1 {_sub} Rd", sale_price=1_500_000, cv_numeric=1_400_000,
        floor_area_m2=150, land_area_m2=500, beds=3, baths=1,
        sold_date=datetime(2026, 7, 15).date()))
_db.commit(); _db.close()

# a name from the SOLD vocabulary must still find the listings
for _n in ("Remuera", "Mount Eden"):
    _m = client.get(f"/api/properties/map?suburb={_n}", headers=H).json()["count"]
    _l = client.get(f"/api/properties?suburb={_n}", headers=H).json()["total"]
    check(f"{_n!r} from one feed matches the other feed's spelling", _m == 1 and _l == 1,
          f"map {_m}, list {_l} — every option matching nothing is a dead control")

# and a name from the LISTINGS vocabulary must work too
check("the qualified spelling works as well",
      client.get("/api/properties?suburb=Remuera, Auckland", headers=H).json()["total"] == 1)

# the fallback must not merge two different suburbs
check("Mount Eden does not drag in Mount Albert",
      client.get("/api/properties?suburb=Mount Eden", headers=H).json()["total"] == 1)
check("Mount Albert is still its own suburb",
      client.get("/api/properties?suburb=Mount Albert", headers=H).json()["total"] == 1)

# a suburb with sales but nothing for sale still returns nothing, not everything
check("a sold-only suburb returns nothing on the listings page",
      client.get("/api/properties?suburb=Papakura", headers=H).json()["total"] == 0)

# and the properties picker no longer offers it in the first place
_live3 = [o["suburb"] for o in client.get("/api/properties/suburbs?dataset=for_sale", headers=H).json()]
check("the properties picker offers only the listings' own vocabulary",
      all("," in n for n in _live3) and "Papakura" not in _live3, f"got {_live3}")


# ---- district and suburb must not be able to contradict each other ---------
# THE actual bug, found by using the thing: pick a district, then pick a suburb
# that is not in it, and the screen empties. Both filters are applied and no
# listing can satisfy both. Nothing on screen says the two disagree, so it reads
# as the suburb filter being broken — which is how it looked for hours.
_db = SessionLocal()
_db.query(M.PropertyForSale).delete()
for _b0 in _db.query(M.ImportBatch).filter(M.ImportBatch.batch_type == "for_sale").all():
    _b0.is_active = False
_db2 = M.ImportBatch(batch_type="for_sale", region="Auckland", filename="dis.csv",
                     is_active=True, status="published",
                     published_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
_db.add(_db2); _db.commit(); _db.refresh(_db2)
for _i, (_sub, _dis) in enumerate((("Remuera", "Auckland City"), ("Mount Eden", "Auckland City"),
                                   ("Papakura", "Papakura"), ("Takapuna", "North Shore"))):
    _db.add(M.PropertyForSale(
        import_batch_id=_db2.id, region="Auckland", suburb=_sub, district=_dis,
        address=f"{_i} District St", asking_price=1_000_000, cv_numeric=1_200_000,
        fair_value=1_300_000, floor_area_m2=140, land_area_m2=600, is_held=False,
        latitude=-36.87, longitude=174.79, beds=3, baths=1))
_db.commit(); _db.close()

_all = [o["suburb"] for o in client.get("/api/properties/suburbs?dataset=for_sale", headers=H).json()]
check("with no district chosen, every suburb is offered",
      sorted(_all) == ["Mount Eden", "Papakura", "Remuera", "Takapuna"], f"got {_all}")

_ak = [o["suburb"] for o in
       client.get("/api/properties/suburbs?dataset=for_sale&district=Auckland City", headers=H).json()]
check("choosing a district narrows the suburb list to that district",
      sorted(_ak) == ["Mount Eden", "Remuera"], f"got {_ak}")
check("a suburb from another district is no longer offered", "Papakura" not in _ak)
check("nor one from a third district", "Takapuna" not in _ak)

_pk = [o["suburb"] for o in
       client.get("/api/properties/suburbs?dataset=for_sale&district=Papakura", headers=H).json()]
check("and each district gets its own suburbs", _pk == ["Papakura"], f"got {_pk}")

# every suburb offered under a district must return listings under that district
for _n in _ak:
    _c = client.get(f"/api/properties/map?district=Auckland City&suburb={_n}",
                    headers=H).json()["count"]
    check(f"{_n!r} under its own district returns listings", _c > 0,
          "an offered combination that returns nothing is the whole bug")

# the contradictory pair is still honoured if something builds it by hand — it
# is a legitimate question with a legitimate answer of "none"
check("a suburb outside the chosen district still returns nothing",
      client.get("/api/properties/map?district=Papakura&suburb=Remuera",
                 headers=H).json()["count"] == 0)
check("but the picker can no longer offer that combination", "Remuera" not in _pk)

# ── the maps key, set in the admin panel instead of at build time ────────────
# It used to be a NEXT_PUBLIC_ variable: rotating it meant a rebuild, and a key
# set without also naming the provider did nothing at all.
_GKEY = "AIza" + "S" * 35
_LKEY = "c" + "0" * 34

check("with nothing set, imagery falls back to the free layer",
      client.get("/api/config/maps", headers=H).json()["provider"] == "esri")
check("and no key is handed out",
      client.get("/api/config/maps", headers=H).json()["google_key"] is None)

check("a non-admin cannot read the admin maps settings",
      client.get("/api/admin/maps").status_code == 401)

_r = client.put("/api/admin/maps", headers=H, json={"google_key": "not-a-key"})
check("an obvious paste error is refused with a reason", _r.status_code == 422, _r.text)
check("and the reason says where to find the real one", "Google Cloud" in _r.text)

_r = client.put("/api/admin/maps", headers=H, json={"provider": "bing"})
check("an unknown provider is refused", _r.status_code == 422, _r.text)

_r = client.put("/api/admin/maps", headers=H, json={"google_key": _GKEY})
check("saving a Google key works", _r.status_code == 200, _r.text)
check("the key alone is enough to switch the provider — no second setting",
      _r.json()["provider"] == "google", _r.json())
check("the panel shows it is configured", _r.json()["google_configured"] is True)
check("with only the last four characters", _r.json()["google_last_four"] == "SSSS")
check("the key itself is never returned to the admin page",
      _GKEY not in _r.text)

_cfg = client.get("/api/config/maps", headers=H).json()
check("the browser gets the key it needs to call Google", _cfg["google_key"] == _GKEY)
check("and is told which provider to use", _cfg["provider"] == "google")
check("but not a key it has no use for", _cfg["linz_key"] is None)

_row = SessionLocal().get(M.AppSetting, M.MAPS_GOOGLE_KEY)
check("the key is encrypted at rest", _row is not None and _GKEY not in (_row.value or ""))

# patch semantics: saving one field must not wipe the other
client.put("/api/admin/maps", headers=H, json={"linz_key": _LKEY})
_r = client.get("/api/admin/maps", headers=H).json()
check("adding a second key leaves the first alone", _r["google_configured"] is True)
check("and records the second", _r["linz_configured"] is True)
check("google still wins when both are set", _r["provider"] == "google")

client.put("/api/admin/maps", headers=H, json={"provider": "linz"})
check("naming a provider overrides the automatic choice",
      client.get("/api/config/maps", headers=H).json()["provider"] == "linz")
check("and hands out that provider's key",
      client.get("/api/config/maps", headers=H).json()["linz_key"] == _LKEY)

# a named provider whose key is gone must not black out every map
client.put("/api/admin/maps", headers=H, json={"linz_key": ""})
check("clearing a key removes it",
      client.get("/api/admin/maps", headers=H).json()["linz_configured"] is False)
check("and the named-but-keyless provider falls through to one that works",
      client.get("/api/config/maps", headers=H).json()["provider"] == "google")

client.put("/api/admin/maps", headers=H, json={"provider": "esri"})
check("esri can be forced even with a Google key saved",
      client.get("/api/config/maps", headers=H).json()["provider"] == "esri")
check("and then no key is sent to the browser at all",
      client.get("/api/config/maps", headers=H).json()["google_key"] is None)

client.put("/api/admin/maps", headers=H, json={"provider": "", "google_key": ""})
check("cleared right back down to the free layer",
      client.get("/api/config/maps", headers=H).json()["provider"] == "esri")

# ── the data probe ────────────────────────────────────────────────────────────
# Answers "what do we get for nothing" against live responses. With no LINZ key
# configured it must say so plainly rather than 500 or claim the data does not
# exist — those are different answers and only one of them is true.
check("the data probe is admin-only",
      client.get("/api/admin/data-probe?address=x").status_code == 401)

_r = client.get("/api/admin/data-probe?address=1+Queen+Street", headers=H)
check("with no LINZ key it refuses with a reason, not a 500",
      _r.status_code == 422, f"{_r.status_code} {_r.text[:120]}")
check("and the reason says where to get one", "data.linz.govt.nz" in _r.text, _r.text[:200])
check("and names the right key, not the imagery one",
      "Basemaps" in _r.text, _r.text[:250])

_r = client.get("/api/admin/data-probe", headers=H)
check("asking with neither an address nor coordinates is refused",
      _r.status_code == 422, _r.status_code)

from app.data_probe import LAYERS as _LAYERS, MODEL_INPUTS as _MI    # noqa: E402
check("the probe asks the four free layers", len(_LAYERS) == 4, len(_LAYERS))
check("including the titles layer that carries the estate type",
      any(l["id"] == "layer-50804" for l in _LAYERS))
check("and scores against all nine model inputs", len(_MI) == 9, len(_MI))
check("CV is on the scorecard", any(m[1] == "Capital value" for m in _MI))

# ── the 401/402 noise ─────────────────────────────────────────────────────────
# A promoter is not a customer, so every paywalled endpoint answers 402 for them.
# That is correct. What was NOT correct was the badge poller in the header
# calling one every 60 seconds and the 402 handler redirecting — a promoter's own
# dashboard bounced them to the paywall a second after it loaded.
_db = SessionLocal()
reset_users(_db)
_pro = M.User(email="promo@example.com", password_hash=_hp("secret123"),
              full_name="P", role="promoter", status="approved")
_adm = M.User(email="a@b.c", password_hash=_hp("x"), full_name="A",
              role="admin", status="approved")
_db.add_all([_pro, _adm]); _db.commit()
_pro_id, _adm_id = _pro.id, _adm.id
_db.close()
_PH = {"Authorization": f"Bearer {create_access_token(_pro_id)[0]}"}

check("a promoter gets 402 from a paywalled endpoint, not 401",
      client.get("/api/wishlists/notifications", headers=_PH).status_code == 402,
      client.get("/api/wishlists/notifications", headers=_PH).status_code)
check("and 402 from the listings too",
      client.get("/api/properties/map", headers=_PH).status_code == 402)
check("but their own dashboard is not paywalled",
      client.get("/api/promoter/dashboard", headers=_PH).status_code in (200, 404),
      client.get("/api/promoter/dashboard", headers=_PH).status_code)

# 401 vs 402 must stay distinguishable: one means "sign in", the other means
# "you are signed in and this costs money". Collapsing them sends a paying
# customer to a login screen.
check("no token is 401, not 402",
      client.get("/api/wishlists/notifications").status_code == 401)

# session length — nothing refreshes a token, so this IS the session
from app.config import settings as _cfg                              # noqa: E402
check("a session lasts a working day, not an hour",
      _cfg.jwt_expiry_minutes >= 480, _cfg.jwt_expiry_minutes)
_tok, _exp = create_access_token(_adm_id)
check("and an issued token carries that expiry",
      (_exp - datetime.now(timezone.utc)).total_seconds() > 8 * 3600,
      (_exp - datetime.now(timezone.utc)).total_seconds())

# neither status may reach the bug log — they are the app working
_before = client.get("/api/admin/bugs", headers=H).json()
client.get("/api/wishlists/notifications")                 # 401
client.get("/api/wishlists/notifications", headers=_PH)    # 402
_after = client.get("/api/admin/bugs", headers=H).json()
check("a 401 does not file itself as a bug", len(_after) == len(_before),
      f"{len(_before)} -> {len(_after)}")

# ── the market pulse ──────────────────────────────────────────────────────────
# PERCENTILE_DISC ... WITHIN GROUP is Postgres-only. On SQLite the whole query
# raised a bare syntax error that was caught and logged, so the pulse was always
# empty in development and in every test — and production being Postgres meant
# nothing looked broken. A number nobody can exercise is a number that ships
# unverified.
_db = SessionLocal()
_pb = M.ImportBatch(batch_type="for_sale", region="Auckland", filename="pulse.csv",
                    rows_total=5, is_active=True)
_db.add(_pb); _db.commit(); _db.refresh(_pb)
for _i, _ask in enumerate((700_000, 900_000, 1_100_000, 1_300_000, 1_500_000)):
    _db.add(M.PropertyForSale(
        import_batch_id=_pb.id, region="Auckland", suburb="Pulse", district="Auckland City",
        address=f"{_i} Pulse Lane", asking_price=_ask, cv_numeric=_ask * 1.1,
        fair_value=_ask * 1.2, floor_area_m2=150, land_area_m2=500,
        beds=3, baths=1, is_held=False, latitude=-36.87, longitude=174.79))
_db.commit(); _db.close()

# A fresh admin token: the 401/402 block above resets the user table, and SQLite
# reuses row ids, so the module-level H can end up pointing at a different
# account entirely.
_db = SessionLocal()
_admin_now = _db.query(M.User).filter(M.User.role == "admin").first()
_AH = {"Authorization": f"Bearer {create_access_token(_admin_now.id)[0]}"}
_db.close()

_brief = client.get("/api/dashboards/today", headers=_AH)
check("the today brief loads", _brief.status_code == 200, _brief.text[:150])
_pulse = _brief.json().get("market_pulse") or {}
check("the market pulse is computed, not silently empty",
      _pulse.get("total_listings", 0) > 0, _pulse)
check("and carries a median asking price", _pulse.get("median_asking") is not None, _pulse)
check("which is the middle listing, not an average",
      _pulse["median_asking"] == 1_100_000, _pulse["median_asking"])

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
