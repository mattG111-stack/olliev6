"""Honest accuracy report for the v4 AVM (Market_Value).

Runs the SAME predictor the app uses (app.pricing.glm.predict) over a CSV of SOLD
properties (rows must carry an actual sale price + CV + the usual features) and
reports the numbers three ways, so a headline like "22% MAPE" can be seen for
what it is:

  1. RAW            — every row, no filter (what an un-filtered report shows)
  2. MARKET-ONLY    — drops non-market sales (sale/CV outside [0.6, 1.4]:
                      family transfers, mortgagee/off-market) — the population
                      the model is actually meant to price
  3. BY PATH        — MARKET-ONLY split into:
                        • asking  (listed → market_value = asking × 0.95)
                        • model   (unlisted → the v3.5 hedonic)
                      so asking-price noise doesn't get blamed on the model.

Targets (per the build spec): asking ~3% MAPE, model ~6-8% MAPE.

Usage:
    python scripts/accuracy_report.py <sold.csv> [--lo 0.6] [--hi 1.4]

The CSV column names are matched loosely (case/space-insensitive) against common
aliases — see COLS below. At minimum it needs a sale price and a CV.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.pricing.glm import predict  # noqa: E402

# Column aliases → canonical name. First match wins (case/space/underscore-insensitive).
COLS = {
    "sold_price": ["sold_price", "sale_price", "price_numeric", "sold", "sold price", "sale price"],
    "cv":         ["cv_numeric", "cv", "council_cv", "capital_value"],
    "asking":     ["asking_price", "asking", "list_price", "price"],
    "address":    ["address", "addr"],
    "suburb":     ["suburb"],
    "district":   ["district"],
    "property_type": ["property_type", "type", "property type"],
    "title":      ["type_of_title", "title", "title_type"],
    "floor":      ["key_floor_area", "floor_area_m2", "floor", "floor_area"],
    "land":       ["key_land_area", "land_area_m2", "land", "land_area"],
    "beds":       ["key_bedrooms", "beds", "bedrooms"],
    "baths":      ["key_bathrooms", "baths", "bathrooms"],
    "cars":       ["key_carspaces", "cars", "carspaces"],
    "age":        ["building_age", "age", "year_built"],
}


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _resolve(df: pd.DataFrame) -> dict:
    lut = {_norm(c): c for c in df.columns}
    out = {}
    for canon, aliases in COLS.items():
        for a in aliases:
            if _norm(a) in lut:
                out[canon] = lut[_norm(a)]
                break
    return out


def _num(v):
    try:
        f = float(str(v).replace(",", "").replace("$", "").strip())
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    ape = np.abs(y_pred - y_true) / y_true
    signed = (y_pred - y_true) / y_true
    return {
        "n":           len(ape),
        "MdAPE %":     float(np.median(ape) * 100),
        "MAPE %":      float(ape.mean() * 100),
        "within ±5%":  float((ape <= 0.05).mean() * 100),
        "within ±10%": float((ape <= 0.10).mean() * 100),
        "within ±20%": float((ape <= 0.20).mean() * 100),
        "mean bias %": float(signed.mean() * 100),
    }


def _show(title: str, y: np.ndarray, p: np.ndarray) -> None:
    print(f"=== {title} ===")
    if len(y) == 0:
        print("  (no rows)\n")
        return
    for k, v in _metrics(y, p).items():
        print(f"  {k:<12} {v:,.0f}" if k == "n" else f"  {k:<12} {v:6.2f}")
    print()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    lo = float(argv[argv.index("--lo") + 1]) if "--lo" in argv else 0.60
    hi = float(argv[argv.index("--hi") + 1]) if "--hi" in argv else 1.40

    df = pd.read_csv(path)
    m = _resolve(df)
    if "sold_price" not in m or "cv" not in m:
        print(f"CSV must have a sale-price and a CV column. Found: {sorted(m)}")
        print(f"Columns in file: {list(df.columns)}")
        return 2
    print(f"Loaded {len(df):,} rows from {path}")
    print(f"Column map: {m}\n")

    ys, ps, cvs, paths = [], [], [], []
    for _, row in df.iterrows():
        sold = _num(row.get(m["sold_price"]))
        cv = _num(row.get(m["cv"]))
        if not sold or not cv or sold <= 0 or cv <= 0:
            continue
        asking = _num(row.get(m["asking"])) if "asking" in m else None
        pred = predict(
            suburb=row.get(m.get("suburb")), district=row.get(m.get("district")),
            property_type=row.get(m.get("property_type")), cv=cv,
            floor=_num(row.get(m.get("floor"))), land=_num(row.get(m.get("land"))),
            beds=_num(row.get(m.get("beds"))), baths=_num(row.get(m.get("baths"))),
            cars=_num(row.get(m.get("cars"))), age=_num(row.get(m.get("age"))),
            title=row.get(m.get("title")), method=None, pool=False,
            address=row.get(m.get("address")),
            asking_price=asking,
            listing_type="fixed" if asking else "unknown",
        )
        if not pred.market_value or pred.market_value <= 0:
            continue
        ys.append(sold); ps.append(pred.market_value)
        cvs.append(cv); paths.append(pred.pricing_path)

    y = np.array(ys, float); p = np.array(ps, float)
    cv = np.array(cvs, float); path = np.array(paths, object)
    print(f"Scored {len(y):,} rows with a prediction\n")

    _show("RAW (every scored row, no filter)", y, p)

    ratio = y / cv
    keep = (ratio >= lo) & (ratio <= hi)
    dropped = (~keep).sum()
    print(f"Market-only filter: dropped {dropped:,} non-market sales "
          f"(sale/CV outside [{lo}, {hi}])\n")
    _show(f"MARKET-ONLY (sale/CV in [{lo}, {hi}])", y[keep], p[keep])

    for name, label, target in (("asking", "asking (listed, ×0.95)", "~3% MAPE"),
                                ("v35", "model (unlisted hedonic)", "~6-8% MAPE")):
        sel = keep & (path == name)
        _show(f"PATH = {label}   target {target}", y[sel], p[sel])

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
