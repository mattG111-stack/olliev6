"""A file you can send someone when the prices look wrong.

WHY THIS EXISTS

The trained valuation moved live prices by -45% to +350%, and it could not be
reproduced anywhere outside production. The committed test fixture carries no
bedroom or bathroom counts, so on it the model shifts valuations by under 2% —
it is incapable of producing the failure. Every diagnosis was therefore a guess
about data nobody outside the running system could see.

That is the actual problem this file solves. Not "export the listings" — there
is already a CSV on the staged review grid for that — but "export enough to
work out WHY a number is what it is", which is a different set of columns:

    what went in        CV, land value, floor, land, beds, baths, title, age
    what is published   the valuation now live, its margin, its confidence
    what the model says its raw prediction, BEFORE the bound is applied
    what happened       whether the model was used, held back, or absent

The raw unbounded prediction is the important one. With the bound in place a
broken model looks fine from the outside — every price sits within 10% of the
previous one — while the model itself is producing nonsense underneath. This
column is how that is visible before it becomes visible in the prices.

NOTHING PERSONAL LEAVES

Property data only: addresses that are public on a listing site, council
figures, areas, room counts. No user, no email, no saved search, no key. The
file is meant to be sent to someone for help, so it must be safe to send.
"""

from __future__ import annotations

import csv
import io

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from ..models import ImportBatch, PropertyForSale
from . import features as F

# Deliberately ordered: identity, then inputs, then what we published, then what
# the model thought. Reading left to right walks the same path the pipeline
# walks, so a wrong number can be traced to the input that caused it.
COLUMNS = [
    "id", "address", "suburb", "district", "property_type", "type_of_title",
    "zoning",
    # inputs
    "cv_numeric", "land_value_numeric", "improvement_value_numeric",
    "floor_area_m2", "land_area_m2", "beds", "baths", "cars", "building_age",
    "asking_price", "listing_type", "days_on_market",
    # what is published now
    "fair_value", "buy_price", "margin", "is_underpriced", "confidence",
    "comps_used", "market_value",
    # what the model says
    "model_prediction", "model_vs_published_pct", "model_status",
    # the two ratios that explain most surprises
    "published_vs_cv_pct", "asking_vs_cv_pct",
]


def _pct(a, b):
    """a/b - 1 as a percentage, or blank."""
    try:
        a, b = float(a), float(b)
        if b == 0 or a != a or b != b:
            return ""
        return round((a / b - 1) * 100, 2)
    except (TypeError, ValueError):
        return ""


def rows(db: Session, region: str = "Auckland",
         batch_id: int | None = None, limit: int = 25_000) -> list[dict]:
    """Every live listing with its inputs, its price, and what the model says."""
    from .store import load as load_model

    q = db.query(PropertyForSale)
    if batch_id is not None:
        q = q.filter(PropertyForSale.import_batch_id == batch_id)
    else:
        active = (db.query(ImportBatch.id)
                  .filter(ImportBatch.is_active, ImportBatch.batch_type == "for_sale")
                  .scalar())
        if active is None:
            return []
        q = q.filter(PropertyForSale.import_batch_id == active)
    recs = q.order_by(PropertyForSale.id).limit(limit).all()
    if not recs:
        return []

    # The model's RAW opinion, unbounded. Computed here rather than read from
    # the row, because the row only ever holds the bounded result and the whole
    # point is to see what the model wanted to do.
    frame = pd.DataFrame([{
        "cv_numeric": p.cv_numeric,
        "land_value_numeric": p.land_value_numeric,
        "key_floor_area": p.floor_area_m2, "key_land_area": p.land_area_m2,
        "key_bedrooms": p.beds, "key_bathrooms": p.baths, "key_carspaces": p.cars,
        "building_age": p.building_age, "type_of_title": p.type_of_title,
        "property_type": p.property_type, "suburb": p.suburb,
        "district": p.district,
    } for p in recs])

    model = load_model(db)
    pred: pd.Series = pd.Series(dtype="float64")
    status_default = "no model fitted"
    if model is not None:
        try:
            pred = model.predict(frame)
            status_default = "model could not price this row"
        except Exception as exc:                          # noqa: BLE001
            status_default = f"model failed: {type(exc).__name__}"

    from ..pricing.pipeline import ML_MAX_SHIFT

    out: list[dict] = []
    for i, p in enumerate(recs):
        mv = pred.get(i) if len(pred) else None
        if mv is not None and mv == mv:
            mv = round(float(mv))
            if p.fair_value:
                shift = abs(mv / p.fair_value - 1)
                status = ("used" if shift <= ML_MAX_SHIFT
                          else f"held back (would move {shift * 100:.1f}%)")
            else:
                status = "no published value to compare"
        else:
            mv, status = "", status_default
        out.append({
            "id": p.id, "address": p.address, "suburb": p.suburb,
            "district": p.district, "property_type": p.property_type,
            "type_of_title": p.type_of_title, "zoning": p.zoning,
            "cv_numeric": p.cv_numeric,
            "land_value_numeric": p.land_value_numeric,
            "improvement_value_numeric": p.improvement_value_numeric,
            "floor_area_m2": p.floor_area_m2, "land_area_m2": p.land_area_m2,
            "beds": p.beds, "baths": p.baths, "cars": p.cars,
            "building_age": p.building_age,
            "asking_price": p.asking_price, "listing_type": p.listing_type,
            "days_on_market": p.days_on_market,
            "fair_value": p.fair_value, "buy_price": p.buy_price,
            "margin": p.margin, "is_underpriced": p.is_underpriced,
            "confidence": p.confidence, "comps_used": p.comps_used,
            "market_value": p.market_value,
            "model_prediction": mv,
            "model_vs_published_pct": _pct(mv, p.fair_value) if mv != "" else "",
            "model_status": status,
            "published_vs_cv_pct": _pct(p.fair_value, p.cv_numeric),
            "asking_vs_cv_pct": _pct(p.asking_price, p.cv_numeric),
        })
    return out


def to_csv(db: Session, region: str = "Auckland",
           batch_id: int | None = None, limit: int = 25_000) -> str:
    data = rows(db, region=region, batch_id=batch_id, limit=limit)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in data:
        w.writerow(r)
    return buf.getvalue()


def summary(db: Session, region: str = "Auckland") -> dict:
    """The one-paragraph version, for when a whole file is more than is needed.

    Answers the question that started this — "why are only nine underpriced" —
    without anyone opening a spreadsheet.
    """
    from .store import active_row, enabled, load as load_model

    data = rows(db, region=region)
    if not data:
        return {"listings": 0, "reason": "no live for-sale batch"}

    df = pd.DataFrame(data)
    fv = pd.to_numeric(df["fair_value"], errors="coerce")
    mp = pd.to_numeric(df["model_prediction"], errors="coerce")
    shift = pd.to_numeric(df["model_vs_published_pct"], errors="coerce")
    row = active_row(db)
    model = load_model(db)

    return {
        "listings": int(len(df)),
        "priced": int(fv.notna().sum()),
        "underpriced": int(df["is_underpriced"].fillna(False).astype(bool).sum()),
        "model_fitted": model is not None,
        "model_in_use": bool(model is not None and enabled(db)),
        "model_trained_on": row.n_train if row else None,
        "model_priced": int(mp.notna().sum()),
        "model_held_back": int((shift.abs() > 10).sum()),
        "model_shift_median_pct": (round(float(shift.median()), 2)
                                   if shift.notna().any() else None),
        "model_shift_worst_down_pct": (round(float(shift.min()), 2)
                                       if shift.notna().any() else None),
        "model_shift_worst_up_pct": (round(float(shift.max()), 2)
                                     if shift.notna().any() else None),
        "listings_missing_floor_area": int(
            pd.to_numeric(df["floor_area_m2"], errors="coerce").isna().sum()),
        "listings_missing_beds": int(
            pd.to_numeric(df["beds"], errors="coerce").isna().sum()),
        "listings_missing_cv": int(
            pd.to_numeric(df["cv_numeric"], errors="coerce").isna().sum()),
    }
