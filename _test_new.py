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
for t in (M.PropertyForSale, M.PropertySold, M.ImportBatch, M.User):
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
_db.query(M.User).delete(); _db.commit()
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
_db.query(M.User).delete()
_db.add(M.User(email="Taken@example.com", password_hash=_hp("secret123"), full_name="T",
               role="user", status="approved"))
_db.commit(); _db.close()
_r = client.post("/api/auth/sign-up", json={"email": "taken@example.com", "password": "secret123",
                                            "full_name": "Imposter"})
check("sign-up rejects an address that differs only by case",
      _r.status_code == 409, f"got {_r.status_code} {_r.text[:120]}")

# a genuine case-only collision is a merge decision, not a repair: leave it alone
_db = SessionLocal()
_db.query(M.User).delete(); _db.commit()
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
for _t in (M.PropertySold, M.ImportBatch):
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
_db = SessionLocal(); _db.query(M.User).delete(); _db.commit(); _db.close()
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

_db = SessionLocal(); _db.query(M.User).delete(); _db.query(M.AssistantLog).delete()
_db.query(M.AppSetting).delete(); _db.commit(); ensure_seed_admin(_db); _db.close()
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

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
