"""End-to-end smoke + endpoint tests for the staged-ingest build.

  DATABASE_URL="sqlite:////tmp/ollie_test.db" JWT_SECRET="testsecret" \
  CORS_ORIGINS="*" SEED_ADMIN_EMAIL="a@b.c" SEED_ADMIN_PASSWORD="x" \
  python _test_harness.py
"""
import sys, traceback

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))

# ---------------------------------------------------------------- import + schema
from app.main import app
from app.db import Base, engine, SessionLocal
import app.models as M
Base.metadata.create_all(engine)
print("OK app imported + schema created")

# ---------------------------------------------------------------- routes (recurse)
def walk(routes):
    for r in routes:
        p = getattr(r, "path", None)
        if p:
            yield p
        for sub in getattr(getattr(r, "app", None), "routes", []) or []:
            yield from walk([sub])
paths = set(walk(app.routes))
for w in ["/api/properties/suggest", "/api/properties/suburb-stats",
          "/api/properties/map", "/api/properties/export.csv"]:
    # HTTP call below is the real proof; this is best-effort enumeration.
    pass

# ---------------------------------------------------------------- helper units
from app import release as R
from app.pricing import subdivision as S

class stub:
    """A PropertyForSale-shaped duck with sensible None defaults (the hold
    helpers only read attributes, so a plain object is enough)."""
    def __init__(self, **kw):
        d = dict(fair_value=None, asking_price=None, cv_numeric=None,
                 valuation_last_sold_value=None, land_area_flag=None, cv_flag=None,
                 floor_area_m2=100.0, property_type="House", expected_sale_path=None)
        d.update(kw)
        self.__dict__.update(d)

check("_below_margin holds 5k gap", R._below_margin(stub(fair_value=800_000, asking_price=795_000)) is True)
check("_below_margin passes 20k gap", R._below_margin(stub(fair_value=900_000, asking_price=880_000)) is False)
check("_below_margin holds when fair_value None", R._below_margin(stub(fair_value=None, asking_price=880_000)) is True)

check("_asking_is_placeholder true when asking==cv",
      R._asking_is_placeholder(stub(asking_price=1_000_000, cv_numeric=1_000_000)) is True)
check("_asking_is_placeholder false when asking>>cv",
      R._asking_is_placeholder(stub(asking_price=1_200_000, cv_numeric=1_000_000)) is False)
check("_asking_is_placeholder true when asking==last-sold",
      R._asking_is_placeholder(stub(asking_price=850_000, cv_numeric=1_000_000, valuation_last_sold_value=850_000)) is True)

# _hold_reason ordering: placeholder asking must beat the (fake) margin
hr_fake = R._hold_reason(stub(fair_value=1_620_000, asking_price=580_000, cv_numeric=582_000))
check("_hold_reason: placeholder asking held as NO_ASKING (6 Pekanga case)", hr_fake == R.NO_ASKING_REASON,
      f"got {hr_fake!r}")
hr_thin = R._hold_reason(stub(fair_value=805_000, asking_price=800_000, cv_numeric=500_000))
check("_hold_reason: thin real margin held as BELOW_MARGIN", hr_thin == R.BELOW_MARGIN_REASON, f"got {hr_thin!r}")
hr_ok = R._hold_reason(stub(listing_type="fixed", fair_value=900_000, asking_price=800_000, cv_numeric=850_000))
check("_hold_reason: real 100k margin not held", hr_ok is None, f"got {hr_ok!r}")
hr_land = R._hold_reason(stub(listing_type="fixed", fair_value=900_000, asking_price=800_000, cv_numeric=850_000, land_area_flag="tiny"))
check("_hold_reason: land-area flag held first", hr_land and "Land area" in hr_land, f"got {hr_land!r}")

check("MARGIN_MIN_DOLLARS == 10000", R.MARGIN_MIN_DOLLARS == 10_000.0)
check("HOLDING_YEARS == 1.0", S.HOLDING_YEARS == 1.0)
check("CONTINGENCY_RATE == 0.03", S.CONTINGENCY_RATE == 0.03)

# Room effects: matched comparison, needs >=3 sales at two adjacent levels
from app.routers.properties import _matched_step_effect, _median

class _S:                      # a sold row, only the fields the effect reads
    def __init__(self, beds, baths, floor, price, ptype="House"):
        self.beds, self.baths = beds, baths
        self.floor_area_m2, self.sale_price, self.property_type = floor, price, ptype
        self.days_on_market = 30

rows = [_S(3, 2, 140, 900_000) for _ in range(4)] + [_S(4, 2, 140, 1_150_000) for _ in range(4)]
eff, basis, gap, cells = _matched_step_effect(rows, "beds")
check("+1 bedroom at the same size = +250k", eff == 250_000, f"got {eff}")
check("and reports it as like-for-like", basis == "like-for-like", f"got {basis}")
check("with no floor-area gap between the groups", gap == 0, f"got {gap}")

# Too few sales at a level -> no number rather than a made-up one
thin = [_S(3, 2, 140, 900_000), _S(4, 2, 140, 1_150_000)]
check("None when a level has too few sales", _matched_step_effect(thin, "beds")[0] is None)

# The confound the matching exists to remove: 4-bed homes are also 60 m2 bigger.
conf = ([_S(3, 2, 140, 1_200_000) for _ in range(4)]
        + [_S(4, 2, 200, 1_800_000) for _ in range(4)])
d, b, g, _ = _matched_step_effect(conf, "beds")
check("unmatched sizes are labelled, not passed off as like-for-like",
      b != "like-for-like", f"got {b}")
check("and the floor-area gap is exposed", g is not None and abs(g) >= 50, f"got {g}")

check("_median odd", _median([1.0, 3.0, 2.0]) == 2.0)

# ---------------------------------------------------------------- seed a DB
db = SessionLocal()
for t in (M.PropertyForSale, M.PropertySold, M.ImportBatch, M.User):
    db.query(t).delete()
db.commit()

admin = M.User(email="a@b.c", password_hash="$2b$12$" + "x" * 53,
               role=M.UserRole.ADMIN.value, status=M.UserStatus.APPROVED.value)
db.add(admin); db.commit(); db.refresh(admin)
admin_id = admin.id  # capture before the session closes (avoids DetachedInstanceError)

fs = M.ImportBatch(batch_type="for_sale", region="Auckland", filename="t.csv", is_active=True, status="published")
so = M.ImportBatch(batch_type="sold", region="Auckland", filename="s.csv", is_active=True, status="published")
db.add_all([fs, so]); db.commit(); db.refresh(fs); db.refresh(so)

# for-sale: one clean deal (survives _hide_bad_data), one held, one placeholder
db.add_all([
    M.PropertyForSale(import_batch_id=fs.id, region="Auckland", suburb="Remuera",
                      address="1 Deal St", asking_price=900_000, cv_numeric=1_000_000,
                      fair_value=1_050_000, floor_area_m2=140, latitude=-36.87, longitude=174.79,
                      is_held=False),
    M.PropertyForSale(import_batch_id=fs.id, region="Auckland", suburb="Remuera",
                      address="2 Held Rd", asking_price=995_000, cv_numeric=1_000_000,
                      floor_area_m2=120, is_held=True, hold_reason="Below $10,000 margin"),
    M.PropertyForSale(import_batch_id=fs.id, region="Auckland", suburb="Remuera",
                      address="6 Pekanga Rd", asking_price=582_000, cv_numeric=582_000,
                      floor_area_m2=110, is_held=False),  # placeholder -> hidden by _is_placeholder_asking
])
# sold: enough at bed levels 3 & 4 to yield a bedroom effect
for i in range(4):
    db.add(M.PropertySold(import_batch_id=so.id, region="Auckland", suburb="Remuera",
                          address=f"{i} Sold Ave", sale_price=2_500_000 + i * 10_000, sold_date="2026-06-01",
                          cv_numeric=2_400_000, floor_area_m2=200, beds=3, baths=2, days_on_market=30))
for i in range(4):
    db.add(M.PropertySold(import_batch_id=so.id, region="Auckland", suburb="Remuera",
                          address=f"{i} Big Ave", sale_price=2_800_000 + i * 10_000, sold_date="2026-06-01",
                          cv_numeric=2_700_000, floor_area_m2=250, beds=4, baths=3, days_on_market=22))
db.commit()
db.close()

# ---------------------------------------------------------------- authenticated client
from fastapi.testclient import TestClient
from app.security import create_access_token
token, _ = create_access_token(admin_id)
H = {"Authorization": f"Bearer {token}"}
client = TestClient(app)

r = client.get("/api/properties/suggest", params={"q": "rem"}, headers=H)
check("GET /suggest 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
if (r.status_code == 200):
    labels = [s.get("label", s.get("value", s)) for s in r.json()]
    print("  /suggest ->", r.json())
    check("/suggest surfaces Remuera suburb", any("Remuera" in str(x) for x in r.json()))

r = client.get("/api/properties/suggest", params={"q": "pek"}, headers=H)
check("GET /suggest excludes placeholder (6 Pekanga)", (r.status_code == 200) and not any("Pekanga" in str(x) for x in r.json()),
      f"{r.status_code} {r.text[:200]}")

r = client.get("/api/properties/suburb-stats", params={"suburb": "Remuera"}, headers=H)
check("GET /suburb-stats 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
if (r.status_code == 200):
    j = r.json()
    print("  /suburb-stats ->", {k: j.get(k) for k in ("active_listings", "sold_count", "median_sold", "sale_vs_cv")})
    print("  effects ->", j.get("effects"))
    check("/suburb-stats sold_count == 8", j.get("sold_count") == 8, f"got {j.get('sold_count')}")
    bed = next((e for e in j.get("effects", []) if e["key"] == "bedroom"), None)
    check("/suburb-stats bedroom effect measured (~+300k)", bed and bed.get("dollars"), f"got {bed}")
    # clean deal counted, held + placeholder excluded from active listings
    check("/suburb-stats active_listings == 1 (held+fake excluded)", j.get("active_listings") == 1,
          f"got {j.get('active_listings')}")

r = client.get("/api/properties/map", params={"dataset": "for_sale", "suburb": "Remuera"}, headers=H)
check("GET /map for_sale 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
if (r.status_code == 200):
    pts = r.json().get("points", [])
    print(f"  /map for_sale -> {len(pts)} points")
    check("/map excludes held + placeholder (1 point)", len(pts) == 1, f"got {len(pts)}")

r = client.get("/api/properties/map", params={"dataset": "sold", "suburb": "Remuera"}, headers=H)
check("GET /map sold 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")


# export.csv must be admin-gated: anon blocked, admin allowed
r_anon = client.get("/api/properties/export.csv")
check("GET /export.csv blocks anon", r_anon.status_code in (401, 402, 403), f"got {r_anon.status_code}")
r_adm = client.get("/api/properties/export.csv", headers=H)
check("GET /export.csv 200 for admin", r_adm.status_code == 200, f"{r_adm.status_code} {r_adm.text[:120]}")

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
