"""Is the trained model actually better than what is running now?

A new model is not an improvement because it is new, and it is not an
improvement because its training error went down — training error goes down when
a model memorises. The only question that matters is how it does on sales it has
never seen, measured against the two things it has to beat:

  raw council CV     the number anyone can read off the council website for free
  the live engine    what the site quotes today, from the May-2026 spreadsheet

Both are computed here on the same held-out rows, so the comparison is like for
like. If the trained model loses, it says so and does not ship — that decision
belongs to `should_ship` below, not to whoever is reading the numbers.

TWO SPLITS, BECAUSE THEY ANSWER DIFFERENT QUESTIONS

A random split answers "does this model generalise". A FORWARD split — fit on
everything before a date, test on everything after — answers "would this model
have worked in production", which is the only way it is ever used. A model can
pass the first and fail the second when it has learned a market that has since
moved, and that failure is invisible to a random split because the future sales
are scattered through the training set.

The forward split is the one that gates shipping.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from . import features as F
from .train import Model, fit

# The margin a new model must win by. Two models within a whisker of each other
# are the same model, and swapping the live valuation for a coin-flip's worth of
# improvement means every listing's price moves for nothing.
MIN_IMPROVEMENT_PCT = 0.15          # percentage POINTS of median error

# Enough held-out sales for the median to mean something.
MIN_TEST_ROWS = 200


@dataclass
class Scores:
    n: int = 0
    model: float = float("nan")       # median % error of the trained model
    raw_cv: float = float("nan")      # median % error of quoting the CV
    engine: float = float("nan")      # median % error of what runs today
    model_p90: float = float("nan")
    within_10pct: float = float("nan")   # share of sales priced within 10%

    def as_dict(self) -> dict:
        return {k: (None if isinstance(v, float) and np.isnan(v) else v)
                for k, v in asdict(self).items()}


def _median_pct(pred: pd.Series, actual: pd.Series) -> float:
    e = (pred - actual).abs() / actual
    e = e.replace([np.inf, -np.inf], np.nan).dropna()
    return float(e.median() * 100) if len(e) else float("nan")


def _engine_prediction(train: pd.DataFrame, test: pd.DataFrame) -> pd.Series:
    """What the live engine would have said, fitted only on the training rows.

    This is CV x the area's shrunk sale/CV ratio — the estimator the site quotes
    from today. Built per split rather than once, because an engine that has
    seen the test sales is not a fair comparison, it is the same leakage the
    trained model is being held to.
    """
    from ..pricing.buyprice import CompEngine

    ce = CompEngine(train)
    cv = F.col(test, "cv")
    out = []
    for i, (_, r) in enumerate(test.iterrows()):
        ratio, _src = ce.cv_ratio_for(
            suburb=str(r.get("suburb") or "").strip(),
            district=str(r.get("district") or "").strip(),
            property_type=r.get("property_type"))
        out.append(float(cv.iloc[i]) * float(ratio) if pd.notna(cv.iloc[i]) else np.nan)
    return pd.Series(out, index=test.index)


def score(train: pd.DataFrame, test: pd.DataFrame, *, seed: int = 0,
          with_engine: bool = True) -> tuple[Scores, Model | None]:
    """Fit on `train`, measure on `test`, against CV and the live engine."""
    keep = F.usable(test)
    test = test[keep]
    if len(test) < MIN_TEST_ROWS:
        return Scores(n=len(test)), None

    model = fit(train, seed=seed)
    pred = model.predict(test)
    actual = F.col(test, "price").reindex(pred.index)
    cv = F.col(test, "cv").reindex(pred.index)

    err = ((pred - actual).abs() / actual).replace([np.inf, -np.inf], np.nan).dropna()
    s = Scores(
        n=int(len(pred)),
        model=_median_pct(pred, actual),
        raw_cv=_median_pct(cv, actual),
        model_p90=float(err.quantile(0.90) * 100) if len(err) else float("nan"),
        within_10pct=float((err <= 0.10).mean() * 100) if len(err) else float("nan"),
    )
    if with_engine:
        eng = _engine_prediction(train, test).reindex(pred.index)
        s.engine = _median_pct(eng, actual)
    return s, model


def random_split(sold: pd.DataFrame, seed: int = 0, frac: float = 0.8):
    idx = np.random.default_rng(seed).permutation(len(sold))
    cut = int(frac * len(idx))
    return sold.iloc[idx[:cut]].copy(), sold.iloc[idx[cut:]].copy()


def forward_split(sold: pd.DataFrame, frac: float = 0.8):
    """Fit on the past, test on the future — how it is actually used.

    Returns (train, test, cut_month). Falls back to a random split with a
    reported reason when the data carries no usable dates, rather than silently
    producing a "forward" split that is nothing of the kind.
    """
    mon = F.months(sold)
    if mon.notna().sum() < MIN_TEST_ROWS * 2:
        a, b = random_split(sold)
        return a, b, None
    cut = float(mon.quantile(frac))
    train = sold[mon <= cut].copy()
    test = sold[mon > cut].copy()
    if len(test) < MIN_TEST_ROWS or len(train) < MIN_TEST_ROWS:
        a, b = random_split(sold)
        return a, b, None
    return train, test, cut


def assess(sold: pd.DataFrame, *, seeds=(0, 7, 42)) -> dict:
    """The full report: forward split first, then random splits for stability."""
    ftrain, ftest, cut = forward_split(sold)
    fwd, model = score(ftrain, ftest, seed=0)

    rnd = []
    for s in seeds:
        a, b = random_split(sold, seed=s)
        sc, _ = score(a, b, seed=s, with_engine=True)
        rnd.append(sc)

    ok = [r for r in rnd if not np.isnan(r.model)]
    return {
        "forward": fwd.as_dict(),
        "forward_cut_month": cut,
        "random": [r.as_dict() for r in rnd],
        "random_mean_model": float(np.mean([r.model for r in ok])) if ok else None,
        "random_mean_engine": float(np.mean([r.engine for r in ok])) if ok else None,
        "random_mean_raw_cv": float(np.mean([r.raw_cv for r in ok])) if ok else None,
        "model": model,
    }


def should_ship(report: dict) -> tuple[bool, str]:
    """Does this model earn the right to replace what is running?

    Three conditions, all on the FORWARD split, because that is the only one
    that resembles production:

      1. it beats raw council CV        — otherwise it is worse than free
      2. it beats the live engine by at least MIN_IMPROVEMENT_PCT points
      3. there were enough held-out sales for the median to mean anything

    Returned as (verdict, sentence) so the sentence can be shown to whoever
    pressed the button. "No" is a normal outcome and reads like one.
    """
    f = report.get("forward") or {}
    n = f.get("n") or 0
    m, cv, eng = f.get("model"), f.get("raw_cv"), f.get("engine")

    if n < MIN_TEST_ROWS or m is None:
        return False, (f"Not enough recent sales to judge it on — {n} held out, "
                       f"need {MIN_TEST_ROWS}. Nothing changed.")
    if cv is not None and m >= cv:
        return False, (f"It is no better than the council figure "
                       f"({m:.2f}% vs {cv:.2f}%), so it stays off. Nothing changed.")
    if eng is not None and m > eng - MIN_IMPROVEMENT_PCT:
        return False, (f"It does not beat what is already running by enough "
                       f"({m:.2f}% vs {eng:.2f}%, needs to win by "
                       f"{MIN_IMPROVEMENT_PCT:.2f} points). Nothing changed.")
    better = f"{eng - m:.2f} points better than the live engine" if eng else \
             f"{cv - m:.2f} points better than the council figure"
    return True, (f"Median error {m:.2f}% on {n} sales it had never seen — "
                  f"{better}. Ready to use.")
