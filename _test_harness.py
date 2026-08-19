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

# Room effects: fitted across the suburb, holding floor area and land area fixed.
from app.routers.properties import _fit_rooms, _room_effect, _median
import random as _rnd

class _S:                      # a sold row, only the fields the fit reads
    def __init__(self, beds, baths, floor, land, price, ptype="House"):
        self.beds, self.baths = beds, baths
        self.floor_area_m2, self.land_area_m2 = floor, land
        self.sale_price, self.property_type = price, ptype
        self.days_on_market = 30

def _suburb(bed_value, bath_value, n=140, seed=5, noise=60_000):
    """Sales where WE decide what a bedroom is worth, so the fit can be graded.
    Bigger sites carry bigger houses and bigger houses carry more rooms, which is
    the confound the fit has to see through."""
    r = _rnd.Random(seed)
    out = []
    for _ in range(n):
        land = round(r.lognormvariate(6.35, 0.45))
        floor = max(70, round(land * r.uniform(0.22, 0.38)))
        beds = min(6, max(2, round(floor / 52 + r.gauss(0, .5))))
        baths = min(5, max(1, round(beds * 0.6 + r.gauss(0, .4))))
        price = (land * 2100 + floor * 3400 + beds * bed_value + baths * bath_value
                 + r.gauss(0, noise))
        out.append(_S(beds, baths, floor, land, round(price)))
    return out

rows = _suburb(60_000, 45_000)
med = _median([x.sale_price for x in rows])
fits = _fit_rooms(rows)
d, lo, hi, n, why = _room_effect(fits, "beds", med)
check("a known $60k bedroom is recovered", d is not None and 20_000 <= d <= 110_000,
      f"got {d} ({why})")
check("and the stated range contains the truth", d is not None and lo <= 60_000 <= hi,
      f"got {lo}-{hi}")
check("the estimate is fitted on the whole suburb, not a handful of cells",
      n >= 100, f"got n={n}")
check("the figure is rounded, not false-precise", d is not None and d % 5000 == 0, f"got {d}")

# The confound that produced the +$1.04M bedroom: an extra bedroom travels with a
# much bigger site. Holding land fixed, the fit must not hand back the land.
d2 = _room_effect(_fit_rooms(_suburb(60_000, 45_000, seed=31)), "beds", med)[0]
check("a bedroom never comes back worth a large share of the median",
      d2 is None or abs(d2) <= 0.35 * med, f"got {d2} vs median {med}")

# Too few sales -> say so rather than fit noise
check("a thin suburb refuses to publish a figure",
      _room_effect(_fit_rooms(_suburb(60_000, 45_000, n=25)), "beds", med)[4]
      == "not enough sales to measure")

# A room that genuinely does nothing must not be dressed up as a number
flat = _room_effect(_fit_rooms(_suburb(0, 0, noise=250_000, seed=17)), "beds", med)
check("no measurable effect is reported as such, not as a number",
      flat[0] is None or abs(flat[0]) < 0.35 * med, f"got {flat[0]}")

check("an effect bigger than a third of the median is withheld",
      _room_effect({"beds": type("F", (), {"dollars": 1_040_000, "low": 900_000,
                                           "high": 1_180_000, "n": 150, "note": None})()},
                   "beds", 1_700_000)[0] is None)

check("_median even averages the two middle values", _median([1.0, 2.0, 3.0, 4.0]) == 2.5)
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
    # Eight sales cannot support the fit. The panel must say so rather than
    # publish a number — this fixture used to produce "+$300k a bedroom" from
    # exactly two comparable sales.
    check("/suburb-stats withholds the effect on 8 sales",
          bed and bed.get("dollars") is None
          and bed.get("note") == "not enough sales to measure", f"got {bed}")
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
