"""Fitting, judging, keeping and loading the trained valuation.

The one place that decides whether a freshly fitted model is allowed to price
anything. Everything else — the endpoint, the pipeline, the tests — asks here.

The rule it enforces: a model may only become active if it beat both raw council
CV and the estimator already running, measured on sales it never saw, on a
FORWARD split. Not "the author thought it looked better", not "training error
went down". A retrain that fails is stored anyway, marked failed, with the
sentence saying why — three failures in a row is how you learn the incoming data
has a problem, and that signal is lost if failures are thrown away.
"""

from __future__ import annotations

import json
import logging

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ML_VALUATION_ENABLED, TrainedModel
from . import evaluate as E
from .train import Model

log = logging.getLogger(__name__)

# One process, one loaded model. Deserialising 17 KB of JSON per property across
# 10,900 listings is 10,900 needless parses; the cache is invalidated by id, so a
# retrain in another worker is picked up rather than served stale forever.
_CACHE: dict[str, tuple[tuple, Model]] = {}


def reset_cache() -> None:
    """Forget the loaded model. For tests, and for anything that swaps the
    database underneath a running process."""
    _CACHE.clear()


def enabled(db: Session) -> bool:
    """Is the trained valuation allowed to price listings? Default YES.

    This started life as an opt-in switch that defaulted to off, and that was
    wrong. The gate in evaluate.should_ship already refuses any model that does
    not beat both the council figure and the estimator already running, on
    sales it never saw, on a forward split. A model only becomes active by
    passing that. Requiring a human to then agree does not add safety — the
    measurement is the decision — it just means a valuation measured as better
    sits unused until somebody remembers to press a button.

    So this is now an OFF OVERRIDE, not an on switch. Absent means use it; only
    an explicit "0" turns it off. That keeps the thing that actually matters —
    you can stop it instantly, and roll back to any earlier model — without
    making "measurably better" wait on an approval nobody is qualified to give
    more precisely than the backtest already did.

    An error reading the setting means the trained model runs, for the same
    reason: the model has passed a stricter test than the estimator it replaces
    ever did, so falling back on a database hiccup would be the more cautious
    step in the less accurate direction.
    """
    from ..settings_store import get as get_setting
    try:
        raw = str(get_setting(db, ML_VALUATION_ENABLED) or "").strip().lower()
    except Exception:                                    # noqa: BLE001
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def set_enabled(db: Session, on: bool, user_id: int | None = None) -> None:
    from ..settings_store import put as put_setting
    put_setting(db, ML_VALUATION_ENABLED, "1" if on else "0", by=user_id)


def active_row(db: Session, kind: str = "valuation") -> TrainedModel | None:
    return db.execute(
        select(TrainedModel)
        .where(TrainedModel.kind == kind, TrainedModel.is_active.is_(True))
        .order_by(TrainedModel.id.desc())
    ).scalars().first()


def load(db: Session, kind: str = "valuation") -> Model | None:
    """The live model, or None if nothing has been fitted and passed yet."""
    row = active_row(db, kind)
    if row is None:
        return None
    # Keyed on more than the row id. An id alone is unique within ONE database,
    # and this cache outlives the database: restore from a backup, or rebuild
    # the schema, and row 1 is a different model with the same id while the
    # process happily keeps serving the old one. Adding trained_at makes a
    # collision require two models fitted in the same second with the same id.
    key = (row.id, str(row.trained_at), row.n_train)
    hit = _CACHE.get(kind)
    if hit and hit[0] == key:
        return hit[1]
    try:
        m = Model.from_json(row.payload)
    except Exception as exc:                             # noqa: BLE001
        # A payload that will not deserialise must not take the site down. The
        # valuation falls back to the engine that was running before, which is
        # exactly what happens when no model has been fitted at all.
        log.error("trained model %s will not load (%s) — falling back", row.id, exc)
        return None
    _CACHE[kind] = (key, m)
    return m


def history(db: Session, limit: int = 20, kind: str = "valuation") -> list[TrainedModel]:
    return list(db.execute(
        select(TrainedModel).where(TrainedModel.kind == kind)
        .order_by(TrainedModel.id.desc()).limit(limit)
    ).scalars())


def train_and_store(db: Session, sold: pd.DataFrame, *,
                    user_id: int | None = None,
                    kind: str = "valuation") -> TrainedModel:
    """Fit, judge, record. Returns the row — shipped or not.

    Raises only when there is nothing to fit at all. A model that fits but loses
    is not an error; it is a result, and it is stored as one.
    """
    report = E.assess(sold)
    model: Model | None = report.pop("model", None)
    if model is None:
        raise ValueError(
            "could not fit a model on this data — not enough sales with both a "
            "price and a usable council valuation")

    ship, verdict = E.should_ship(report)
    fwd = report.get("forward") or {}
    model.metrics = {**(model.metrics or {}), "report": report, "verdict": verdict}

    row = TrainedModel(
        kind=kind,
        payload=model.to_json(),
        metrics=json.dumps(report, default=str),
        n_train=model.n_train,
        n_test=fwd.get("n"),
        forward_error=fwd.get("model"),
        engine_error=fwd.get("engine"),
        raw_cv_error=fwd.get("raw_cv"),
        shipped=bool(ship),
        verdict=verdict[:255],
        is_active=False,
        trained_by_id=user_id,
    )
    db.add(row)
    db.flush()

    if ship:
        # Exactly one active model per kind. Done as an explicit sweep rather
        # than trusting that only one was ever set: a half-finished earlier
        # retrain leaving two active rows would make which model prices a
        # listing depend on row order, which is the kind of bug that takes a
        # week to see.
        for other in db.execute(
            select(TrainedModel).where(TrainedModel.kind == kind,
                                       TrainedModel.is_active.is_(True))
        ).scalars():
            other.is_active = False
        row.is_active = True
        _CACHE.pop(kind, None)

    db.commit()
    return row


def rollback_to(db: Session, model_id: int, kind: str = "valuation") -> TrainedModel:
    """Make an earlier model live again.

    Only a model that passed its gate can be reactivated — a failed one was
    measured as worse than what it would replace, and "roll back to the one we
    already know is bad" is never the intended request.
    """
    row = db.get(TrainedModel, model_id)
    if row is None or row.kind != kind:
        raise ValueError(f"no model {model_id}")
    if not row.shipped:
        raise ValueError(
            f"model {model_id} never passed its gate ({row.verdict}) — "
            f"reactivating it would put back a valuation measured as worse")
    for other in db.execute(
        select(TrainedModel).where(TrainedModel.kind == kind,
                                   TrainedModel.is_active.is_(True))
    ).scalars():
        other.is_active = False
    row.is_active = True
    _CACHE.pop(kind, None)
    db.commit()
    return row


def live_model(db: Session, kind: str = "valuation") -> "Model | None":
    """The model that should price listings right now, or None.

    One call, used by every path that prices anything, so ingest and re-price
    cannot end up disagreeing about which valuation is in force — two listings
    priced by two different models, in the same batch, is a bug nobody would
    spot from the numbers.

    Best-effort: any failure here means the previous estimator runs, which is
    what happened before there was a model at all.
    """
    try:
        if not enabled(db):
            return None
        return load(db, kind)
    except Exception:                                    # noqa: BLE001
        log.exception("could not load the trained valuation — using the previous one")
        return None
