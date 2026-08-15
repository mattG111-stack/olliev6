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

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)


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
