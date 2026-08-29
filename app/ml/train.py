"""Fitting a valuation on our own sales.

WHY THIS EXISTS

The valuation's coefficients came out of `Algo data 17-05-2026.xlsx`. They were
fitted once, elsewhere, on data we do not hold, and they have not moved since.
Every week since then real Auckland sales have landed in our database and taught
the model nothing. A fixed coefficient is a claim that the market has not
changed, restated confidently every time a page loads.

WHY RIDGE AND NOT A FOREST

No ML library is installed and adding one is not free: this container has been
OOM-killed twice, and the app's resident set is already 127 MB before a model is
loaded. scikit-learn plus its dependencies is a large addition for a fit that
is, in this problem, a solved linear algebra problem.

More importantly, a log-linear hedonic is the SAME functional form the v3.5 GLM
already uses. That makes this a like-for-like swap — same features, same shape,
our data instead of a spreadsheet's — which is a claim the backtest can settle
cleanly. A gradient-boosted model would change two things at once and leave it
unclear which one moved the number. That comes next, if this wins and there is
still headroom.

Ridge rather than plain least squares because the columns are correlated by
construction (floor area and CV and bedroom count all measure "how much house"),
and unpenalised coefficients on collinear inputs swing wildly between refits.
The penalty is chosen by cross-validation rather than picked.

WHY SUBURB IS A RESIDUAL EFFECT AND NOT A COLUMN

Three hundred suburbs as one-hot columns is a matrix that is 95% zeros, and the
suburbs with four sales would fit those four sales exactly. Instead the base
model is fitted first, and what it gets WRONG per suburb is measured and shrunk
toward the district, and the district toward zero — a suburb with 200 sales
carries nearly all of its own correction, one with 3 carries almost none.

This is the same credibility idea as K_SUBURB/K_DISTRICT in buyprice.py, applied
to residuals instead of ratios. The effects are computed from the TRAINING rows
only, which is what stops a sale from helping to predict itself.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from . import features as F

# Credibility constants for the residual effects. A suburb needs K_SUBURB sales
# before it carries half its own correction. Matched to buyprice.py so the two
# layers do not disagree about how much a thin suburb is trusted.
K_SUBURB = 30
K_DISTRICT = 20
K_TYPE = 15

# Penalties tried by cross-validation. Spanning six orders of magnitude because
# the right one depends on how many sales and how correlated the columns are,
# and both change every time a file lands.
LAMBDAS = (0.01, 0.1, 1.0, 10.0, 100.0, 1000.0)
CV_FOLDS = 5

# A learned model may not invent or destroy value wholesale. Whatever the fit
# says, a prediction outside this band of the council valuation is a bug in the
# model or a broken record in the input, and shipping it would be worse than
# quoting CV. Matches the clamp the pricing pipeline already applies.
PRED_VS_CV = (0.35, 3.0)

# Below this there is not enough evidence to fit anything. A model fitted on 200
# sales that then prices 10,900 listings is not a model, it is an anecdote.
MIN_TRAIN_ROWS = 400


@dataclass
class Effects:
    """Shrunk residual corrections, by group."""
    suburb: dict[str, float] = field(default_factory=dict)
    district: dict[str, float] = field(default_factory=dict)
    ctype: dict[str, float] = field(default_factory=dict)


@dataclass
class Model:
    """Everything needed to reproduce a prediction, and nothing that isn't.

    Serialisable on purpose: the fit happens in a background job and the
    prediction happens in a request, possibly in a different process after a
    redeploy. A model that only exists in the memory of the process that fitted
    it is a model that silently reverts to the spreadsheet on every restart.
    """
    names: list[str]
    coef: list[float]
    intercept: float
    medians: dict[str, float]
    base_month: float
    lam: float
    # The newest month the fit saw. A listing has no sale date, so it is valued
    # at TODAY's market rather than at the oldest month in the training window.
    latest_month: float = 0.0
    effects: Effects = field(default_factory=Effects)
    n_train: int = 0
    trained_at: str = ""
    metrics: dict = field(default_factory=dict)

    # -- prediction --------------------------------------------------------
    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Predicted sale price per row, indexed like the rows it could use.

        Rows the model cannot speak to — no price-sane CV, nothing to anchor on —
        are simply absent from the result rather than being given a number. A
        caller that wants a value for every row must decide what to do about
        that; quietly returning a guess is how an unpriceable property ends up
        looking like a priced one.
        """
        d = F.build(df, medians=self.medians, base_month=self.base_month,
                    training=False, latest_month=self.latest_month)
        if len(d.rows) == 0:
            return pd.Series(dtype="float64")

        # Column order is part of the model. Rebuilding by name rather than by
        # position means a future feature added to features.py cannot silently
        # shift every coefficient onto the wrong column.
        idx = {n: i for i, n in enumerate(d.names)}
        X = np.column_stack([
            d.X[:, idx[n]] if n in idx else np.zeros(len(d.rows))
            for n in self.names
        ])
        ln = X @ np.asarray(self.coef, dtype="float64") + self.intercept

        subs = d.rows["suburb"].astype(str).str.strip()
        dists = d.rows["district"].astype(str).str.strip() if "district" in d.rows \
            else pd.Series("", index=d.rows.index)
        ln = ln + np.array([
            self.effects.suburb.get(s, self.effects.district.get(dd, 0.0))
            for s, dd in zip(subs, dists)
        ])
        ln = ln + d.rows["_ctype"].map(lambda c: self.effects.ctype.get(str(c), 0.0)) \
                                  .to_numpy(dtype="float64")

        pred = pd.Series(np.exp(ln), index=d.rows.index)

        # The clamp. Never silent: a clamped row is a row the model got badly
        # wrong, and the count of them is reported in the metrics.
        cv = F.col(d.rows, "cv")
        lo, hi = cv * PRED_VS_CV[0], cv * PRED_VS_CV[1]
        return pred.clip(lower=lo, upper=hi)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d)

    @classmethod
    def from_json(cls, s: str) -> "Model":
        d = json.loads(s)
        d["effects"] = Effects(**(d.get("effects") or {}))
        return cls(**d)


# ---------------------------------------------------------------------------
# the fit
# ---------------------------------------------------------------------------

def _ridge(X: np.ndarray, y: np.ndarray, lam: float) -> tuple[np.ndarray, float]:
    """Centred ridge. Returns (coefficients, intercept).

    Centring rather than adding a column of ones, because the intercept must NOT
    be penalised — penalising it shrinks every prediction toward zero rather
    than toward the mean, which on log prices is a systematic underestimate that
    grows with the penalty.
    """
    xm, ym = X.mean(axis=0), float(y.mean())
    Xc, yc = X - xm, y - ym
    n_feat = Xc.shape[1]
    A = Xc.T @ Xc + lam * np.eye(n_feat)
    b = Xc.T @ yc
    try:
        coef = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        coef = np.linalg.lstsq(A, b, rcond=None)[0]
    return coef, ym - float(xm @ coef)


def _pick_lambda(X: np.ndarray, y: np.ndarray, seed: int = 0) -> tuple[float, dict]:
    """K-fold cross-validation over LAMBDAS, scored on held-out log error."""
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(y))
    folds = np.array_split(order, CV_FOLDS)
    scores: dict[float, float] = {}
    for lam in LAMBDAS:
        errs = []
        for k in range(CV_FOLDS):
            test = folds[k]
            train = np.concatenate([folds[j] for j in range(CV_FOLDS) if j != k])
            coef, b0 = _ridge(X[train], y[train], lam)
            pred = X[test] @ coef + b0
            errs.append(float(np.median(np.abs(np.expm1(pred - y[test])))))
        scores[lam] = float(np.mean(errs))
    best = min(scores, key=scores.get)
    return best, {str(k): round(v, 5) for k, v in scores.items()}


def _shrunk(resid: pd.Series, keys: pd.Series, k: int,
            fallback: dict[str, float] | None = None,
            parent: pd.Series | None = None) -> dict[str, float]:
    """Mean residual per group, pulled toward its parent by sample size.

    weight = n / (n + k). A group with k sales carries half its own correction.
    """
    out: dict[str, float] = {}
    df = pd.DataFrame({"r": resid.to_numpy(), "g": keys.astype(str).str.strip().to_numpy()})
    if parent is not None:
        df["p"] = parent.astype(str).str.strip().to_numpy()
    grouped = df.groupby("g")["r"]
    for g, series in grouped:
        n = len(series)
        w = n / (n + k)
        base = 0.0
        if parent is not None and fallback:
            pg = df.loc[df["g"] == g, "p"]
            if len(pg):
                base = fallback.get(pg.iloc[0], 0.0)
        out[g] = float(w * series.mean() + (1 - w) * base)
    return out


def fit(sold: pd.DataFrame, *, seed: int = 0) -> Model:
    """Fit a valuation on these sales.

    Raises ValueError when there is not enough to fit — deliberately loud. A
    trainer that quietly returns a model fitted on eleven rows is worse than one
    that refuses, because the eleven-row model will be used.
    """
    d = F.build(sold)
    if len(d.y) < MIN_TRAIN_ROWS:
        raise ValueError(
            f"only {len(d.y)} usable sales — need {MIN_TRAIN_ROWS} to fit "
            f"anything worth using ({d.dropped['unusable_rows']} rows had no "
            f"price, no CV, or a sale/CV ratio outside {F.SANE_CV_RATIO})")

    lam, cv_scores = _pick_lambda(d.X, d.y, seed=seed)
    coef, b0 = _ridge(d.X, d.y, lam)

    # What the base fit gets wrong, per area and per type. Computed on the
    # training rows only — a sale must never help predict itself.
    resid = pd.Series(d.y - (d.X @ coef + b0), index=d.rows.index)
    dist = d.rows["district"].astype(str).str.strip() if "district" in d.rows else \
        pd.Series("", index=d.rows.index)
    eff_dist = _shrunk(resid, dist, K_DISTRICT)
    eff_sub = _shrunk(resid, d.rows["suburb"], K_SUBURB,
                      fallback=eff_dist, parent=dist)
    eff_type = _shrunk(resid, d.rows["_ctype"], K_TYPE)

    from datetime import datetime, timezone
    return Model(
        names=list(d.names), coef=[float(c) for c in coef], intercept=float(b0),
        medians=d.medians, base_month=d.base_month, lam=float(lam),
        latest_month=d.latest_month,
        effects=Effects(suburb=eff_sub, district=eff_dist, ctype=eff_type),
        n_train=int(len(d.y)),
        trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        metrics={"cv_scores": cv_scores, "chosen_lambda": lam,
                 "unusable_rows": d.dropped["unusable_rows"],
                 "features": len(d.names)},
    )
