"""Loads the v3.5/v3.8 trained reference tables from JSON on app startup.

The JSON files are produced by `scripts/extract_v38_tables.py` from the client's
`Algo data 17-05-2026.xlsx` workbook. Loading them once at module import gives
the pricing engine fast in-memory lookups for every property.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

TABLES_DIR = Path(__file__).resolve().parent / "v38_tables"
# The same eleven tables in one file. Kept as a bundle because app/ has to stay
# under the 100-file ceiling GitHub's web uploader imposes, and eleven separate
# JSONs were a tenth of the budget for data that is always read together.
BUNDLE_PATH = Path(__file__).resolve().parent / "v38_tables.json"

_BUNDLE: dict | None = None


def _bundle() -> dict:
    """The bundled tables, read once. An unreadable bundle is not fatal — the
    individual files are still supported, so a checkout with either layout
    works and neither has to be migrated in step with the other."""
    global _BUNDLE
    if _BUNDLE is None:
        try:
            with open(BUNDLE_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            _BUNDLE = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            _BUNDLE = {}
    return _BUNDLE


def _load(name: str):
    """One table, from the bundle if it is there and the loose file if not.

    Deliberately not bundle-only: a deployment that still has the old
    v38_tables/ directory and no bundle must keep pricing correctly rather than
    fall back to defaults, which would change every valuation silently.
    """
    bundled = _bundle()
    if name in bundled:
        return bundled[name]
    path = TABLES_DIR / f"{name}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# Multiplicative correction factors should be near 1.0. If a row in the source
# workbook is mis-extracted (e.g. a dollar amount picked up instead of a ratio),
# the GLM exp() will blow up by 5-6 orders of magnitude. Clamp to safe range.
_CORR_MIN, _CORR_MAX = 0.3, 3.0


def _sanitise_corr(d: dict) -> dict:
    out: dict = {}
    for k, v in d.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            out[k] = 1.0
            continue
        if not (_CORR_MIN <= f <= _CORR_MAX):
            f = 1.0
        out[k] = f
    return out


# Same clamp for CV-ratio tables (they multiply CV to produce an anchor — a
# 6-figure value here would produce planet-scale anchors).
_CV_RATIO_MIN, _CV_RATIO_MAX = 0.3, 3.0


def _sanitise_cv_ratio(d: dict) -> dict:
    out: dict = {}
    for k, v in d.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            out[k] = 1.0
            continue
        if not (_CV_RATIO_MIN <= f <= _CV_RATIO_MAX):
            f = 1.0
        out[k] = f
    return out


@dataclass(frozen=True)
class V38Tables:
    # β coefficients: {key -> {"n": int, "beta": [15 floats]}}
    beta_subtype: dict[str, dict]      # "Suburb||Type" -> {n, beta}
    beta_suburb: dict[str, dict]        # "Suburb"     -> {n, beta}
    beta_district: dict[str, dict]      # "District"   -> {n, beta}
    beta_global: dict                   # {n, beta}

    # Correction factors
    suburb_corr: dict[str, float]
    subtype_corr: dict[str, float]      # "Suburb||Type" -> factor
    type_corr: dict[str, float]
    building_corr: dict[str, float]     # "building_key||suburb" lowercased
    district_type_corr: dict[str, float]

    # CV-anchor blend
    cv_ratio_subtype: dict[str, float]  # "Suburb||Type"
    cv_ratio_suburb: dict[str, float]
    cv_ratio_type: dict[str, float]
    cv_ratio_global: float

    # Bucket sample counts (drives Z)
    bucket_n: dict[str, int]            # "Suburb||Type"


def load_tables() -> V38Tables:
    cv = _load("cv_ratios")
    cv_global = cv.get("global") or 1.0
    if not (_CV_RATIO_MIN <= float(cv_global) <= _CV_RATIO_MAX):
        cv_global = 1.0
    return V38Tables(
        beta_subtype=_load("beta_subtype"),
        beta_suburb=_load("beta_suburb"),
        beta_district=_load("beta_district"),
        beta_global=_load("beta_global"),
        suburb_corr=_sanitise_corr(_load("suburb_corr")),
        subtype_corr=_sanitise_corr(_load("subtype_corr")),
        type_corr=_sanitise_corr(_load("type_corr")),
        building_corr=_sanitise_corr(_load("building_corr")),
        district_type_corr=_sanitise_corr(_load("district_type_corr")),
        cv_ratio_subtype=_sanitise_cv_ratio(cv.get("subtype", {})),
        cv_ratio_suburb=_sanitise_cv_ratio(cv.get("suburb", {})),
        cv_ratio_type=_sanitise_cv_ratio(cv.get("type", {})),
        cv_ratio_global=float(cv_global),
        bucket_n=_load("bucket_n"),
    )


# Singleton instance — load once on import.
TABLES = load_tables()
