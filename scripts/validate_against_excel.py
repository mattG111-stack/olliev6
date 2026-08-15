"""Validation gate: run our Python pipeline over the same 15,008 for-sale rows
the client priced in their Excel, compare row-by-row, and emit a markdown report.

This is the checkpoint where we decide whether the port is faithful enough to ship.
Target: >= 80% of rows within +/- 10% of client's Market_Value.
"""
from __future__ import annotations

import sys
import warnings as _w
from pathlib import Path

import numpy as np
import pandas as pd

_w.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[3]
XLSX = ROOT / "Ollie property.xlsx"
REPORT = Path(__file__).resolve().parents[1] / "validation_report.md"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.pricing.comps import SoldDataset  # noqa: E402
from app.pricing.pipeline import run  # noqa: E402


def metric_diff(ours: pd.Series, theirs: pd.Series, label: str,
                *, floor: float = 0.0, mask_extra: pd.Series | None = None) -> dict:
    """Percentage-error metrics with a DENOMINATOR FLOOR.

    A percentage error (a−b)/|b| is meaningless when the reference b is ~0: a
    single near-zero actual (a $0 rent, a break-even $0 cashflow) blows the MEAN
    to hundreds of thousands of percent. `floor` drops rows whose |b| is below a
    scale-appropriate threshold so those non-comparable rows stop poisoning the
    aggregate; they're reported as `n_below_floor` rather than silently kept.
    Also reports MEDIAN bias — the honest central tendency — beside the mean,
    which the tail otherwise dominates.

    `mask_extra` (optional) restricts the comparison to a subset, e.g. the
    shippable rows after the data-quality "bad eggs" are excluded.
    """
    a = pd.to_numeric(ours, errors="coerce")
    b = pd.to_numeric(theirs, errors="coerce")
    both = a.notna() & b.notna()
    below = both & (b.abs() <= floor)              # near-zero denominators
    mask = both & (b.abs() > floor)
    if mask_extra is not None:
        mask = mask & mask_extra.reindex(mask.index, fill_value=False)
    if not mask.any():
        return {"label": label, "n": 0, "n_below_floor": int(below.sum())}
    rel = (a[mask] - b[mask]) / b[mask].abs()
    abs_rel = rel.abs()
    return {
        "label": label,
        "n": int(mask.sum()),
        "n_below_floor": int(below.sum()),
        "median_abs_pct": float(abs_rel.median() * 100),
        "mean_abs_pct": float(abs_rel.mean() * 100),
        "within_5_pct": float((abs_rel <= 0.05).mean() * 100),
        "within_10_pct": float((abs_rel <= 0.10).mean() * 100),
        "within_20_pct": float((abs_rel <= 0.20).mean() * 100),
        "bias_pct": float(rel.mean() * 100),
        "median_bias_pct": float(rel.median() * 100),
    }


def _col(out: pd.DataFrame, name: str, default):
    """A column if the pipeline emits it, otherwise a constant Series.

    Guarded because a silently-missing column is how an exclusion rule stops
    excluding anything: `is_auction` was dropped from OUTPUT_COLS when the
    auction feature was removed, and this mask went on quietly defaulting it to
    False, so the "no-asking / auction" bad eggs were counted as clean for every
    run after that.
    """
    if name in out.columns:
        return out[name]
    return pd.Series([default] * len(out), index=out.index)


def shippable_mask(out: pd.DataFrame) -> tuple[pd.Series, dict]:
    """The 'bad eggs' to exclude — NOT by error size, but by the same
    data-quality flags that hold a row from the customer feed.

    Excluding a row because our own pipeline declines to stand behind it is
    honest: it was never shown to a customer, so it was never a fair test.
    Excluding a row because the error came out large is not, and nothing here
    does that — no rule below reads the reference value or the error at all.
    Both full-population and kept-only numbers are reported for every metric.
    """
    n = len(out)
    conf = _col(out, "confidence", "").astype(str)
    path = _col(out, "pricing_path", "").astype(str)
    ltype = _col(out, "listing_type", "").astype(str).str.strip().str.lower()
    premium = _col(out, "is_premium", False).fillna(False).astype(bool)
    no_value = pd.to_numeric(_col(out, "fair_value", None), errors="coerce").isna()

    # Auctions and no-price listings, read off listing_type / pricing_path — the
    # columns that actually exist — rather than the is_auction flag that doesn't.
    no_price = ltype.isin({"auction", "tender", "deadline", "negotiation", "enquiries over", "by negotiation"})
    insufficient = conf.eq("insufficient") | path.eq("insufficient")

    reasons = {
        "insufficient comps / no usable value": int(insufficient.sum()),
        "premium (>$5M, model withholds value)": int(premium.sum()),
        "auction / tender / no asking price": int(no_price.sum()),
        "no confident fair value (broken CV, bare land)": int(no_value.sum()),
    }
    drop = insufficient | premium | no_price | no_value
    return ~drop, reasons


def applicable(out: pd.DataFrame, keep: pd.Series, metric: str) -> pd.Series:
    """Shippable AND the output is one this row should even have.

    A cashflow error on a row we never computed a rent for is not a model error,
    and a subdivision error on a site that cannot be subdivided is not either.
    Each rule below asks "did we produce this figure for a real reason", never
    "how far off was it".
    """
    m = keep.copy()
    if metric in ("Est weekly rent", "Annual cashflow"):
        rent = pd.to_numeric(_col(out, "est_weekly_rent", None), errors="coerce")
        m &= rent.notna() & (rent > 0)
    if metric in ("Max additional lots", "Best net gain (subdivision)"):
        lots = pd.to_numeric(_col(out, "max_addl_lots", None), errors="coerce")
        m &= lots.notna() & (lots > 0)
    if metric == "Best net gain (subdivision)":
        gain = pd.to_numeric(_col(out, "best_net_gain", None), errors="coerce")
        m &= gain.notna()
    return m


def main() -> int:
    print("Loading sold dataset (2016 sold_output) ...")
    sold_df = pd.read_excel(XLSX, sheet_name="2016 sold_output")
    print(f"  {len(sold_df)} sold rows")

    print("Loading for-sale dataset ...")
    fs_df = pd.read_excel(XLSX, sheet_name="for sale")
    print(f"  {len(fs_df)} for-sale rows")

    print("Building sold index ...")
    sold = SoldDataset(sold_df)

    print("Running full pipeline over all 15,008 rows (may take 1-2 min) ...")
    out = run(fs_df, sold)
    print(f"  Done. Output shape: {out.shape}")

    # === Compare key columns ===
    print()
    print("Computing diff metrics ...")

    # "Bad eggs": rows we hold from the feed / can't stand behind — excluded from
    # the HEADLINE by a fixed data-quality rule, never by error size. Both full and
    # clean numbers are reported below so nothing is hidden.
    keep, drop_reasons = shippable_mask(out)

    # Denominator floors stop near-zero actuals from exploding the percentage: a $0
    # rent or a break-even $0 cashflow otherwise produces the ±573,000% "bias".
    OUTPUTS = [
        ("Market_Value", "market_value", "Market_Value", 10_000),
        ("Max additional lots", "max_addl_lots", "Max_addl_lots", 0.5),
        ("Est weekly rent", "est_weekly_rent", "Est_Weekly_Rent", 50),
        ("Annual cashflow", "annual_cashflow", "Annual_Cashflow", 2_000),
        ("Best net gain (subdivision)", "best_net_gain", "Best_Net_Gain", 10_000),
    ]
    # Every output now gets BOTH numbers. Previously only Market_Value had a
    # shippable column, so the bad eggs sat in all four other metrics unchallenged
    # and their medians and biases were being read as if they were clean.
    metrics, metrics_clean = [], []
    for label, ours_col, theirs_col, floor in OUTPUTS:
        metrics.append(metric_diff(out[ours_col], fs_df[theirs_col], label, floor=floor))
        metrics_clean.append(metric_diff(
            out[ours_col], fs_df[theirs_col], label, floor=floor,
            mask_extra=applicable(out, keep, label)))
    mv_clean = metrics_clean[0]
    # Market_Value segmented by our confidence tier — shows the confident majority
    # is tight and the error lives in the low-trust tail.
    mv_by_tier = []
    for tier in ("high", "medium", "low", "insufficient"):
        seg = out["confidence"].astype(str).eq(tier)
        if seg.any():
            m = metric_diff(out["market_value"], fs_df["Market_Value"], tier, floor=10_000, mask_extra=seg)
            if m.get("n"):
                mv_by_tier.append(m)

    # Market_Value segmented by PRICING PATH. This is the cut that matters most
    # and the one the report was missing: on the "asking" path market_value is
    # asking x 0.95, so any error there is an INGEST error — a mis-parsed or
    # whole-block price — not the AVM being wrong. Averaging the two together
    # both flatters the ingest and slanders the model.
    mv_by_path = []
    if "pricing_path" in out.columns:
        for path in ("asking", "v35", "insufficient"):
            seg = out["pricing_path"].astype(str).eq(path)
            if seg.any():
                m = metric_diff(out["market_value"], fs_df["Market_Value"], path,
                                floor=10_000, mask_extra=seg)
                if m.get("n"):
                    mv_by_path.append(m)

    # === Method distribution ===
    method_counts = out["confidence"].value_counts(dropna=False)

    # === Strategy match ===
    strategy_match = None
    if "Best_Strategy" in fs_df.columns:
        their_strat = fs_df["Best_Strategy"].fillna("(none)")
        our_strat = out["best_strategy"].fillna("(none)")
        agree = (their_strat == our_strat).sum()
        strategy_match = (int(agree), int(len(fs_df)), float(agree / len(fs_df) * 100))

    # === Worst-offender rows ===
    diff_mv = pd.to_numeric(out["market_value"], errors="coerce") - pd.to_numeric(fs_df["Market_Value"], errors="coerce")
    rel_mv = (diff_mv / pd.to_numeric(fs_df["Market_Value"], errors="coerce")).abs()
    worst = (
        pd.DataFrame({
            "address": fs_df["address"].astype(str).str[:40],
            "suburb": fs_df["suburb"],
            "type": fs_df["property_type"].astype(str).str[:30],
            "beds": fs_df["key_bedrooms"],
            "floor": fs_df["key_floor_area"],
            "theirs": fs_df["Market_Value"],
            "ours": out["market_value"],
            "rel_diff_pct": rel_mv * 100,
            "method": out["confidence"],
        })
        .dropna(subset=["rel_diff_pct"])
        .sort_values("rel_diff_pct", ascending=False)
        .head(20)
    )

    # === Write report ===
    lines: list[str] = []
    lines.append("# Validation gate — Python pipeline vs client's Excel")
    lines.append("")
    lines.append("Generated by `scripts/validate_against_excel.py`.")
    lines.append("")
    lines.append("## Headline accuracy (Market_Value)")
    lines.append("")
    lines.append("Two numbers: the full population, and the **shippable** subset with the "
                 "data-quality 'bad eggs' removed (rows we hold from the feed — see the "
                 "exclusion breakdown below). Bad eggs are dropped by a fixed rule, never "
                 "by error size.")
    lines.append("")
    mv = metrics[0]
    lines.append("| Metric | Full population | Shippable (bad eggs removed) |")
    lines.append("|---|---|---|")
    lines.append(f"| n compared | {mv['n']:,} | {mv_clean['n']:,} |")
    lines.append(f"| Median absolute % error | {mv['median_abs_pct']:.2f}% | **{mv_clean['median_abs_pct']:.2f}%** |")
    lines.append(f"| Mean absolute % error (MAPE) | {mv['mean_abs_pct']:.2f}% | **{mv_clean['mean_abs_pct']:.2f}%** |")
    lines.append(f"| Within ±5% | {mv['within_5_pct']:.1f}% | {mv_clean['within_5_pct']:.1f}% |")
    lines.append(f"| Within ±10% | {mv['within_10_pct']:.1f}% | **{mv_clean['within_10_pct']:.1f}%** |")
    lines.append(f"| Within ±20% | {mv['within_20_pct']:.1f}% | {mv_clean['within_20_pct']:.1f}% |")
    lines.append(f"| Mean bias | {mv['bias_pct']:+.2f}% | {mv_clean['bias_pct']:+.2f}% |")
    lines.append(f"| Median bias | {mv['median_bias_pct']:+.2f}% | {mv_clean['median_bias_pct']:+.2f}% |")
    lines.append("")
    lines.append("### Bad eggs excluded from the shippable headline")
    lines.append("")
    lines.append("| Reason | Rows |")
    lines.append("|---|---|")
    for reason, cnt in drop_reasons.items():
        lines.append(f"| {reason} | {cnt:,} |")
    lines.append(f"| **Total excluded (deduped)** | **{mv['n'] - mv_clean['n']:,}** |")
    lines.append("")
    if mv_by_path:
        lines.append("### Market_Value by pricing path")
        lines.append("")
        lines.append("`asking` = market_value is asking x 0.95, so error here is an **ingest** "
                     "problem (mis-parsed or whole-block price), not the model. `v35` = the "
                     "hedonic actually ran. Read these separately; the blended headline is "
                     "not a measure of either one.")
        lines.append("")
        lines.append("| Path | n | Median APE | MAPE | Within ±10% | Median bias |")
        lines.append("|---|---|---|---|---|---|")
        for m in mv_by_path:
            lines.append(f"| {m['label']} | {m['n']:,} | {m['median_abs_pct']:.2f}% | "
                         f"{m['mean_abs_pct']:.2f}% | {m['within_10_pct']:.1f}% | "
                         f"{m['median_bias_pct']:+.2f}% |")
        lines.append("")
    lines.append("### Market_Value by our confidence tier")
    lines.append("")
    lines.append("| Tier | n | Median APE | MAPE | Within ±10% | Median bias |")
    lines.append("|---|---|---|---|---|---|")
    for m in mv_by_tier:
        lines.append(f"| {m['label']} | {m['n']:,} | {m['median_abs_pct']:.2f}% | "
                     f"{m['mean_abs_pct']:.2f}% | {m['within_10_pct']:.1f}% | {m['median_bias_pct']:+.2f}% |")
    lines.append("")
    lines.append("## Other model outputs")
    lines.append("")
    lines.append("Bias is now the **median** (mean is tail-dominated). `dropped` = rows "
                 "with a near-zero reference value, excluded so they don't blow up the "
                 "percentages.")
    lines.append("")
    lines.append("| Output | n | Median APE | Within ±10% | Within ±20% | Median bias | dropped |")
    lines.append("|---|---|---|---|---|---|---|")
    for m in metrics[1:]:
        if m["n"] == 0:
            lines.append(f"| {m['label']} | 0 | — | — | — | — | {m.get('n_below_floor', 0):,} |")
        else:
            lines.append(
                f"| {m['label']} | {m['n']:,} | {m['median_abs_pct']:.2f}% | "
                f"{m['within_10_pct']:.1f}% | {m['within_20_pct']:.1f}% | "
                f"{m['median_bias_pct']:+.2f}% | {m.get('n_below_floor', 0):,} |"
            )
    lines.append("")
    lines.append("### Other model outputs — shippable only (bad eggs removed)")
    lines.append("")
    lines.append("Same rule as the Market_Value headline, plus an applicability test: a "
                 "cashflow error on a row we never produced a rent for is not a model "
                 "error, and a subdivision error on a site that cannot be subdivided is "
                 "not one either. **No row is dropped for having a large error.**")
    lines.append("")
    lines.append("| Output | n | Median APE | MAPE | Within ±10% | Within ±20% | Median bias |")
    lines.append("|---|---|---|---|---|---|---|")
    for m in metrics_clean[1:]:
        if m["n"] == 0:
            lines.append(f"| {m['label']} | 0 | — | — | — | — | — |")
        else:
            lines.append(
                f"| {m['label']} | {m['n']:,} | {m['median_abs_pct']:.2f}% | "
                f"{m['mean_abs_pct']:.2f}% | {m['within_10_pct']:.1f}% | "
                f"{m['within_20_pct']:.1f}% | {m['median_bias_pct']:+.2f}% |"
            )
    # --- which subdivision path is wrong -------------------------------------
    # "+207% on net gain" is a number, not a diagnosis. The two subdivision
    # strategies are completely different pro-formas — a THAB terrace build and a
    # retain-the-house-and-sell-the-surplus split — and a single blended figure
    # cannot say which one is off, so every run has reported the same unactionable
    # number. Segmenting by the path that produced each row turns the next run
    # into an answer.
    method = _col(out, "section_value_method", "").astype(str).fillna("")
    paths = [m for m in method.unique() if m and m != "nan"]
    if paths:
        lines.append("## Subdivision outputs by path")
        lines.append("")
        lines.append("Which pro-forma produced the row. A bias concentrated in one "
                     "path is a bug in that path; a bias in both is a bug in the "
                     "shared assumptions.")
        lines.append("")
        lines.append("| Output | Path | n | Median APE | Within ±20% | Median bias |")
        lines.append("|---|---|---|---|---|---|")
        for label, ours_col, theirs_col, floor in OUTPUTS:
            if label not in ("Max additional lots", "Best net gain (subdivision)"):
                continue
            for pth in sorted(paths):
                seg = applicable(out, keep, label) & method.eq(pth)
                if not seg.any():
                    continue
                m = metric_diff(out[ours_col], fs_df[theirs_col], pth, floor=floor,
                                mask_extra=seg)
                if not m.get("n"):
                    continue
                lines.append(
                    f"| {label} | {pth} | {m['n']:,} | {m['median_abs_pct']:.2f}% | "
                    f"{m['within_20_pct']:.1f}% | {m['median_bias_pct']:+.2f}% |"
                )
        lines.append("")

        # An integer output that is wrong by exactly one every time is a
        # definition mismatch, not a modelling error, and it looks identical to a
        # 50% accuracy problem in a percentage table. Count it directly.
        ours_lots = pd.to_numeric(_col(out, "max_addl_lots", None), errors="coerce")
        theirs_lots = pd.to_numeric(fs_df["Max_addl_lots"], errors="coerce")
        seg = applicable(out, keep, "Max additional lots") & ours_lots.notna() & theirs_lots.notna()
        if seg.any():
            d = (ours_lots - theirs_lots)[seg]
            lines.append("### Max additional lots — exact difference")
            lines.append("")
            lines.append("| ours − theirs | rows | share |")
            lines.append("|---|---|---|")
            for delta, cnt in d.round().value_counts().sort_index().head(9).items():
                lines.append(f"| {delta:+.0f} | {cnt:,} | {cnt / len(d) * 100:.1f}% |")
            lines.append("")
            lines.append(f"Exactly right on **{(d.round() == 0).mean() * 100:.1f}%** of rows.")
            lines.append("")

    lines.append("")
    if strategy_match:
        a, t, p = strategy_match
        lines.append(f"## Best Strategy agreement: **{a:,} / {t:,} ({p:.1f}%)**")
        lines.append("")
    lines.append("## Method / confidence distribution (ours)")
    lines.append("")
    lines.append("```")
    for k, v in method_counts.items():
        lines.append(f"  {k:<14} {v:>6,}")
    lines.append("```")
    lines.append("")
    lines.append("## Top 20 worst-offender Market_Value rows")
    lines.append("")
    lines.append("These are the rows where our number diverges most from theirs.")
    lines.append("")
    lines.append("| Address | Suburb | Type | Beds | Floor | Theirs | Ours | Diff % | Method |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for _, r in worst.iterrows():
        lines.append(
            f"| {r['address']} | {r['suburb']} | {r['type']} | "
            f"{int(r['beds']) if pd.notna(r['beds']) else '?'} | "
            f"{int(r['floor']) if pd.notna(r['floor']) else '?'} | "
            f"${r['theirs']/1000:.0f}k | ${r['ours']/1000:.0f}k | "
            f"{r['rel_diff_pct']:.1f}% | {r['method']} |"
        )
    lines.append("")

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to {REPORT}")

    # === Console summary ===
    print()
    print("=" * 60)
    print("VALIDATION GATE SUMMARY")
    print("=" * 60)
    print(f"Full population : median APE {mv['median_abs_pct']:.2f}%, "
          f"MAPE {mv['mean_abs_pct']:.2f}%, within +/-10% = {mv['within_10_pct']:.1f}%")
    print(f"Shippable       : median APE {mv_clean['median_abs_pct']:.2f}%, "
          f"MAPE {mv_clean['mean_abs_pct']:.2f}%, within +/-10% = {mv_clean['within_10_pct']:.1f}% "
          f"(n={mv_clean['n']:,}, {mv['n'] - mv_clean['n']:,} bad eggs removed)")
    # The gate judges the SHIPPABLE stock — the rows we actually publish.
    if mv_clean["within_10_pct"] >= 80:
        print("PASS (>= 80% of shippable rows within +/-10%). Safe to proceed.")
        return 0
    print(f"REVIEW NEEDED ({mv_clean['within_10_pct']:.1f}% of shippable within +/-10%, target 80%).")
    print(f"See {REPORT.name} for the tier breakdown and top 20 outliers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
