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

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
