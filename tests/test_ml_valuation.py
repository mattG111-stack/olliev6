"""A valuation fitted on our own sales, and the gate that decides if it ships.

    "we need full Machine learning around ollie"

The audit that started this: there was no machine learning. The valuation ran a
hedonic GLM whose coefficients were extracted from `Algo data 17-05-2026.xlsx` —
fitted once, elsewhere, on data we do not hold. No training script existed. No ML
library was installed. Sold files landed every week and moved not one coefficient.

A fixed coefficient is a claim that the market has not changed, restated
confidently on every page load.

What these tests are really guarding is not the arithmetic — numpy can be trusted
to solve a linear system. It is the two ways an ML feature goes quietly wrong:

  LEAKAGE — a model that has seen the answer scores brilliantly and then fails in
  production. The suburb effects are the dangerous part, because they are fitted
  from residuals and it would be very easy to compute them over all the data.

  SHIPPING A LOSER — "new model, therefore better" is how accuracy regresses
  without anyone noticing. The gate below refuses on a tie, refuses when the test
  set is too small to mean anything, and refuses when the model cannot beat the
  number anyone can read off the council website for free.

Measured on the committed fixture of 10,168 real Auckland sales, forward split at
five cut points: the trained model beat the live engine 5/5, mean 7.59% vs 7.88%
median error. On random splits it is a tie — which is the finding, not a
footnote. The advantage is specifically at predicting FORWARD, because the
trained model carries a time trend and current suburb effects and the spreadsheet
is frozen in May 2026. That advantage grows as the spreadsheet ages.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from app.ml import evaluate as E
from app.ml import features as F
from app.ml import store
from app.ml.train import MIN_TRAIN_ROWS, Model, fit
from app.models import TrainedModel, User


def _sales(n=1200, seed=0, **over):
    """Synthetic sales with a KNOWN price rule, so a wrong fit is detectable.

    price = CV x exp(suburb effect + 0.10*ln(floor/190) + 0.05*(beds-4) + noise)

    Written as a MULTIPLE OF CV on purpose. The first version of this fixture
    was an intercept plus 0.55*ln(cv), which produces prices around $2.6bn — and
    every row was then correctly thrown out by the sale/CV sanity filter, so
    fifteen tests failed at once on a fixture that was nonsense rather than on
    code that was wrong. Real sales sit within a whisker of their council
    valuation; a fixture that does not is not testing the thing it claims to.
    """
    rng = np.random.default_rng(seed)
    subs = ["Riverhead", "Glenfield", "Papakura", "Mount Albert"]
    eff = {"Riverhead": 0.18, "Glenfield": 0.0, "Papakura": -0.22, "Mount Albert": 0.10}
    s = rng.choice(subs, n)
    cv = rng.lognormal(14.0, 0.35, n)
    floor = rng.normal(190, 45, n).clip(60, 500)
    beds = rng.integers(2, 6, n)
    ln = (np.log(cv) + 0.04 + 0.10 * np.log(floor / 190.0) + 0.05 * (beds - 4)
          + np.array([eff[x] for x in s]) + rng.normal(0, 0.06, n))
    df = pd.DataFrame({
        "suburb": s,
        "district": ["Rodney" if x == "Riverhead" else "Auckland City" for x in s],
        "property_type": "House",
        "type_of_title": "1.0",
        "sale_price": np.exp(ln),
        "cv_numeric": cv,
        "floor_area_m2": floor,
        "land_area_m2": rng.normal(700, 150, n).clip(200, 2000),
        "beds": beds,
        "baths": rng.integers(1, 4, n),
        "sold_date": [f"2026-{rng.integers(1, 7):02d}-14" for _ in range(n)],
    })
    for k, v in over.items():
        df[k] = v
    return df


# ---- it learns something real ----------------------------------------------
def test_it_fits_and_recovers_a_known_price_rule():
    df = _sales(1500)
    m = fit(df)
    pred = m.predict(df)
    err = ((pred - df["sale_price"].reindex(pred.index)).abs()
           / df["sale_price"].reindex(pred.index)).median()
    assert err < 0.10, f"median error {err:.1%} on data with a known rule"


def test_it_learns_the_suburb_differences_rather_than_averaging_them():
    """The whole point of the residual effects. If Papakura and Riverhead come
    out the same, the model is quoting a regional average at every property."""
    m = fit(_sales(2000))
    assert m.effects.suburb["Riverhead"] > m.effects.suburb["Papakura"]
    assert m.effects.suburb["Riverhead"] - m.effects.suburb["Papakura"] > 0.15


def test_a_thin_suburb_is_pulled_toward_its_district():
    """Four sales must not carry a full correction — that is fitting noise and
    calling it local knowledge."""
    df = _sales(1200)
    thin = df.head(4).copy()
    thin["suburb"] = "Tiny Place"
    thin["district"] = "Rodney"
    thin["sale_price"] = thin["sale_price"] * 3.0        # wildly off
    m = fit(pd.concat([df, thin], ignore_index=True))
    # Shrunk with K_SUBURB=30, four sales carry 4/34 of their own signal.
    assert abs(m.effects.suburb.get("Tiny Place", 0.0)) < 0.35


def test_it_refuses_to_fit_on_too_little():
    """A model fitted on eleven rows will be used. Refusing is the safe answer."""
    with pytest.raises(ValueError, match="usable sales"):
        fit(_sales(50))


# ---- the failure modes that hide ------------------------------------------
def test_suburb_effects_come_from_training_rows_only():
    """Leakage check. If the effects were fitted over everything, a test sale
    would help predict itself and the held-out score would be a fiction."""
    df = _sales(2000)
    train, test = E.random_split(df, seed=3)
    m = fit(train)
    only_in_test = set(test["suburb"]) - set(train["suburb"])
    for s in only_in_test:
        assert s not in m.effects.suburb


def test_a_missing_value_is_a_feature_not_a_silent_zero():
    """1,900 of 10,168 real sales carry no land area. Dropping them throws away
    a fifth of the evidence, and it is not a random fifth."""
    df = _sales(1200)
    df.loc[df.index[:300], "land_area_m2"] = np.nan
    m = fit(df)
    assert "missing_ln_land" in m.names
    pred = m.predict(df)
    assert len(pred) == len(df), "rows with a missing area must still be priced"
    assert pred.notna().all()


def test_imputation_medians_travel_with_the_model():
    """Filling a blank at predict time with a different number than the fit was
    told makes the coefficient mean something else. The medians are part of the
    model, not part of the training run."""
    m = fit(_sales(1200))
    assert m.medians and "ln_floor" in m.medians
    round_tripped = Model.from_json(m.to_json())
    assert round_tripped.medians == m.medians
    assert round_tripped.base_month == m.base_month


def test_a_prediction_can_never_run_away_from_the_council_valuation():
    """Whatever the fit says, quoting 8x CV is a bug reaching a customer."""
    df = _sales(1200)
    m = fit(df)
    odd = df.head(20).copy()
    odd["cv_numeric"] = 100_000.0                        # far from anything fitted
    pred = m.predict(odd)
    assert (pred <= 100_000 * 3.0 + 1).all()
    assert (pred >= 100_000 * 0.35 - 1).all()


def test_the_model_survives_a_round_trip_through_json():
    """It is fitted in a background job and used in a request, possibly after a
    redeploy. A model that only exists in memory reverts to the spreadsheet."""
    m = fit(_sales(1200))
    again = Model.from_json(m.to_json())
    a, b = m.predict(_sales(300, seed=9)), again.predict(_sales(300, seed=9))
    pd.testing.assert_series_equal(a, b)


# ---- the gate --------------------------------------------------------------
def _report(model_err, engine_err, cv_err, n=800):
    return {"forward": {"n": n, "model": model_err, "engine": engine_err,
                        "raw_cv": cv_err}}


def test_a_clear_winner_ships():
    ok, why = E.should_ship(_report(7.0, 7.9, 8.0))
    assert ok and "Ready to use" in why


def test_a_model_no_better_than_the_council_figure_does_not_ship():
    ok, why = E.should_ship(_report(8.1, 7.9, 8.0))
    assert not ok
    assert "council figure" in why and "Nothing changed" in why


def test_a_tie_does_not_ship():
    """Swapping the live valuation for a coin flip moves every price for nothing."""
    ok, why = E.should_ship(_report(7.88, 7.90, 8.0))
    assert not ok
    assert "does not beat" in why


def test_too_few_held_out_sales_does_not_ship():
    ok, why = E.should_ship(_report(6.0, 7.9, 8.0, n=12))
    assert not ok and "Not enough recent sales" in why


def test_the_verdict_is_a_sentence_a_person_can_read():
    for rep in (_report(7.0, 7.9, 8.0), _report(8.1, 7.9, 8.0), _report(6.0, 7.9, 8.0, n=5)):
        _, why = E.should_ship(rep)
        assert why[0].isupper() and why.endswith(".")
        assert "%" in why or "sales" in why


# ---- storing, activating, rolling back -------------------------------------
def test_a_winner_becomes_the_active_model(db_session):
    row = store.train_and_store(db_session, _sales(2500))
    assert row.shipped and row.is_active
    assert row.forward_error is not None
    assert store.load(db_session) is not None


def test_only_one_model_is_ever_active(db_session):
    store.train_and_store(db_session, _sales(2500, seed=1))
    store.train_and_store(db_session, _sales(2500, seed=2))
    active = [r for r in store.history(db_session) if r.is_active]
    assert len(active) == 1


def test_a_failed_retrain_is_kept_and_does_not_become_active(db_session):
    """Three failures in a row is how you learn the incoming data has a
    problem. Throwing failures away loses that signal."""
    good = store.train_and_store(db_session, _sales(2500, seed=1))
    row = TrainedModel(kind="valuation", payload=good.payload, shipped=False,
                       is_active=False, verdict="It is no better. Nothing changed.")
    db_session.add(row)
    db_session.commit()
    assert row.id is not None
    assert store.active_row(db_session).id == good.id


def test_rolling_back_to_a_model_that_never_passed_is_refused(db_session):
    good = store.train_and_store(db_session, _sales(2500, seed=1))
    bad = TrainedModel(kind="valuation", payload=good.payload, shipped=False,
                       verdict="worse")
    db_session.add(bad)
    db_session.commit()
    with pytest.raises(ValueError, match="never passed its gate"):
        store.rollback_to(db_session, bad.id)


def test_rolling_back_to_an_earlier_winner_works(db_session):
    first = store.train_and_store(db_session, _sales(2500, seed=1))
    store.train_and_store(db_session, _sales(2500, seed=2))
    back = store.rollback_to(db_session, first.id)
    assert back.is_active
    assert store.active_row(db_session).id == first.id


def test_a_corrupt_payload_falls_back_instead_of_breaking_the_site(db_session):
    store.reset_cache()
    db_session.add(TrainedModel(kind="valuation", payload="{not json",
                                shipped=True, is_active=True))
    db_session.commit()
    assert store.load(db_session) is None


# ---- the switch ------------------------------------------------------------
def test_the_trained_valuation_does_not_price_anything_until_switched_on(db_session):
    """This has been both ways. The history is the argument.

    It was made auto-on because should_ship already refuses a model that does
    not beat both the council figure and the estimator running today. Sound
    reasoning, but only if the gate is sufficient - and it was not. It measured
    MEDIAN error, the median says nothing about the tails, and a model that
    passed it moved live prices by -45% to +350%.

    There is a hard bound now (ML_MAX_SHIFT) that makes that impossible. This
    default is the belt to its braces: a +0.28 point accuracy gain does not earn
    the right to turn itself on.
    """
    store.train_and_store(db_session, _sales(2500))
    assert store.enabled(db_session) is False         # fitted, measured, idle
    store.set_enabled(db_session, True)
    assert store.enabled(db_session) is True
    store.set_enabled(db_session, False)
    assert store.enabled(db_session) is False


# ---- guards on the honest-measurement machinery ----------------------------
def test_broken_records_never_reach_the_fit():
    """A sale at 8x its CV is a family transfer or a mis-keyed record, not
    evidence about the market. Training on it bakes it into every coefficient."""
    df = _sales(1200)
    df.loc[df.index[:40], "sale_price"] = df["cv_numeric"].iloc[:40] * 8
    keep = F.usable(df)
    assert keep.sum() == len(df) - 40


def test_the_forward_split_really_splits_on_time():
    df = _sales(3000)
    df["sold_date"] = [f"2026-{(i % 6) + 1:02d}-14" for i in range(len(df))]
    train, test, cut = E.forward_split(df)
    assert cut is not None
    assert F.months(train).max() <= cut < F.months(test).min()


def test_the_forward_split_says_so_when_it_cannot_split_on_time():
    """Silently returning a random split labelled 'forward' would make the gate
    a lie — the thing it exists to measure would not have been measured."""
    df = _sales(1500)
    df["sold_date"] = None
    _, _, cut = E.forward_split(df)
    assert cut is None


# ---- the endpoint, which unit tests do not reach --------------------------
#
# The first version of /api/admin/ml/train called create_stage_job(user_id=...)
# and that argument does not exist — the function takes uploaded_by_id and
# region. Every test above passed, because none of them called the endpoint;
# the smoke test walks GETs only, and this is a POST. It surfaced on the first
# real HTTP request, as a 500.
#
# So: call it the way a browser does.

@pytest.fixture()
def admin_client(db_session):
    """An authenticated admin client, the way the smoke test builds one."""
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import UserRole, UserStatus
    from app.security import current_user, require_active, require_admin

    admin = User(email="ml-admin@test.local", password_hash="x",
                 role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(admin)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: admin,
        require_active: lambda: admin,
        require_admin: lambda: admin,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}


def test_the_train_endpoint_starts_a_job_rather_than_500ing(admin_client):
    r = admin_client.post("/api/admin/ml/train")
    assert r.status_code == 200, r.text
    assert isinstance(r.json().get("job_id"), int)


def test_status_reports_nothing_fitted_before_any_training(admin_client):
    r = admin_client.get("/api/admin/ml/status")
    assert r.status_code == 200, r.text
    assert r.json()["has_model"] is False
    # "enabled" means IN USE, which needs something fitted to use.
    assert r.json()["enabled"] is False


def test_turning_it_on_with_nothing_fitted_is_refused_in_words(admin_client):
    """A switch that silently enables nothing is an afternoon wondering why
    prices did not move."""
    r = admin_client.post("/api/admin/ml/enabled", json={"enabled": True})
    assert r.status_code == 400
    assert "train one first" in r.json()["detail"]


def test_rolling_back_to_a_model_that_does_not_exist_is_a_400(admin_client):
    r = admin_client.post("/api/admin/ml/rollback/999999")
    assert r.status_code == 400


# ---- the wiring, which is what makes any of this matter -------------------
#
# 9.94 shipped the trainer, the storage, the endpoints and the admin panel, and
# never connected the model to the code that prices houses. Nothing in the
# pricing path imported app/ml at all. The switch controlled nothing: turning it
# on would have changed exactly zero prices, and every number on the site would
# still have come from the May-2026 spreadsheet.
#
# These are the tests that would have caught that.

def test_the_pricing_pipeline_accepts_a_model():
    import inspect

    from app.pricing.pipeline import run
    assert "model" in inspect.signature(run).parameters


def test_the_model_actually_changes_a_published_price():
    """The test 9.94 did not have, and the one that matters most.

    The first wiring attempt substituted the model into the v3.5 hedonic. It
    looked correct, the pipeline logged "trained valuation priced 40 of 40
    rows", and every published price came back byte-identical - because the
    number a customer sees is not the hedonic, it is CV x the area's sale/CV
    ratio, computed elsewhere in pipeline.run.

    "The model is loaded" and "the model is used" are different claims. This
    asserts the second one.
    """
    from app.pricing.comps import SoldDataset
    from app.pricing.pipeline import run as run_pipeline

    sold = _sales(3000, seed=4).rename(columns={
        "sale_price": "price_numeric", "floor_area_m2": "key_floor_area",
        "land_area_m2": "key_land_area", "beds": "key_bedrooms",
        "baths": "key_bathrooms"})
    sold["address"] = [f"{i} Sold St" for i in range(len(sold))]
    sold["land_value_numeric"] = sold["cv_numeric"] * 0.55
    sold["building_age"] = 2005

    live = _sales(40, seed=11).rename(columns={
        "floor_area_m2": "key_floor_area", "land_area_m2": "key_land_area",
        "beds": "key_bedrooms", "baths": "key_bathrooms"}).drop(columns=["sale_price"])
    live["address"] = [f"{i} Live Rd" for i in range(len(live))]
    live["price_numeric"] = None
    live["price_display"] = "Auction"
    live["key_carspaces"] = 2
    live["land_value_numeric"] = None
    live["building_age"] = 2005
    live["has_swimming_pool"] = False

    sd = SoldDataset(sold)
    model = fit(sold)
    before = run_pipeline(live.copy(), sd)
    after = run_pipeline(live.copy(), sd, model=model)

    a = pd.to_numeric(before["fair_value"], errors="coerce")
    b = pd.to_numeric(after["fair_value"], errors="coerce")
    both = a.notna() & b.notna()
    assert both.sum() >= 10, "the fixture priced too few listings to judge"
    moved = ((b[both] - a[both]).abs() / a[both] > 0.001).sum()
    assert moved > 0, ("the model is loaded and priced every row, and not one "
                       "published price changed - it is not wired in")


def test_the_hedonic_is_not_where_the_model_goes():
    """A parameter that looks wired is worse than no parameter.

    glm.predict briefly took a trained_value. It was passed, it was used, and
    it moved nothing a customer could see. Removing it keeps the next person
    from making the same wiring look correct.
    """
    import inspect

    from app.pricing.glm import predict
    assert "trained_value" not in inspect.signature(predict).parameters


def test_prediction_does_not_filter_listings_by_their_asking_price():
    """The bug that would have silently skipped every deal.

    On a for-sale row price_numeric is the ASKING price. The training filter
    drops rows whose price/CV is outside 0.3-3.0 — apply that at predict time
    and the model refuses to value exactly the listings asking far below their
    council valuation, which is the entire product.
    """
    m = fit(_sales(1500))
    listings = _sales(200, seed=5).rename(columns={"sale_price": "price_numeric"})
    listings["price_numeric"] = listings["cv_numeric"] * 0.15    # a screaming deal
    pred = m.predict(listings)
    assert len(pred) == len(listings), "the model refused to price the deals"


def test_a_listing_with_no_asking_price_at_all_is_still_valued():
    m = fit(_sales(1500))
    listings = _sales(50, seed=6).drop(columns=["sale_price"])
    assert len(m.predict(listings)) == 50


# ---- and it turns itself on ------------------------------------------------
def test_fitting_a_model_changes_no_price_by_itself(db_session):
    """Fit, measure, store - and touch nothing. The model becomes ACTIVE when it
    passes its gate, which is a claim about accuracy; it starts PRICING only
    when somebody switches it on, which is a decision about risk. Those are two
    different things and conflating them is what broke production."""
    row = store.train_and_store(db_session, _sales(2500))
    assert row.shipped and row.is_active               # it passed
    assert store.live_model(db_session) is None        # and it is not pricing


def test_it_can_still_be_switched_off_instantly(db_session):
    """Auto-on is only defensible while stopping it is one click."""
    store.train_and_store(db_session, _sales(2500))
    store.set_enabled(db_session, False)
    assert store.enabled(db_session) is False
    assert store.live_model(db_session) is None
    store.set_enabled(db_session, True)
    assert store.live_model(db_session) is not None


def test_nothing_fitted_means_no_model_regardless_of_the_switch(db_session):
    """"On" with nothing fitted must not pretend there is something to use."""
    assert store.live_model(db_session) is None
    store.set_enabled(db_session, True)
    assert store.live_model(db_session) is None


def test_live_model_never_raises_into_the_pricing_path(db_session):
    """A pricing run must not die because the model would not load."""
    store.reset_cache()
    db_session.add(TrainedModel(kind="valuation", payload="{broken",
                                shipped=True, is_active=True))
    db_session.commit()
    assert store.live_model(db_session) is None


# ---- the bound, which is the whole safety story ---------------------------
#
# Shipped unbounded, the trained valuation moved real prices by -45% to +350% in
# a single re-price. A Saint Marys Bay house went from $2.72M to $1.50M. A
# Henderson one from $1.01M to $4.55M. The model had PASSED its gate: its median
# error was genuinely better.
#
# That is the lesson. The gate measures the MEDIAN, and the median says nothing
# about the tails. A model can improve the middle of the distribution while
# producing individual numbers that are indefensible — and it is the individual
# numbers that reach a customer, not the median.
#
# So the model may refine the valuation, not rewrite it.

def test_the_model_cannot_move_a_valuation_more_than_the_bound():
    from app.pricing.comps import SoldDataset
    from app.pricing.pipeline import ML_MAX_SHIFT, run as run_pipeline

    sold = _sales(3000, seed=4).rename(columns={
        "sale_price": "price_numeric", "floor_area_m2": "key_floor_area",
        "land_area_m2": "key_land_area", "beds": "key_bedrooms",
        "baths": "key_bathrooms"})
    sold["address"] = [f"{i} Sold St" for i in range(len(sold))]
    sold["land_value_numeric"] = sold["cv_numeric"] * 0.55
    sold["building_age"] = 2005

    live = _sales(120, seed=12).rename(columns={
        "floor_area_m2": "key_floor_area", "land_area_m2": "key_land_area",
        "beds": "key_bedrooms", "baths": "key_bathrooms"}).drop(columns=["sale_price"])
    live["address"] = [f"{i} Live Rd" for i in range(len(live))]
    live["price_numeric"] = None
    live["price_display"] = "Auction"
    live["key_carspaces"] = 2
    live["land_value_numeric"] = None
    live["building_age"] = 2005
    live["has_swimming_pool"] = False

    sd = SoldDataset(sold)

    class Wild:
        """A model that has gone completely wrong — 4x on everything."""
        n_train = 9999
        def predict(self, df):
            cv = pd.to_numeric(df["cv_numeric"], errors="coerce")
            return (cv * 4.0).dropna()

    before = run_pipeline(live.copy(), sd)
    after = run_pipeline(live.copy(), sd, model=Wild())

    a = pd.to_numeric(before["fair_value"], errors="coerce")
    b = pd.to_numeric(after["fair_value"], errors="coerce")
    both = a.notna() & b.notna()
    assert both.sum() > 10
    shift = ((b[both] - a[both]).abs() / a[both])
    assert shift.max() <= ML_MAX_SHIFT + 1e-6, (
        f"a broken model moved a valuation by {shift.max():.1%} — the bound "
        f"is not holding, and this is exactly how -45% and +350% reached a "
        f"customer")


def test_a_model_inside_the_bound_is_still_used():
    """The bound must not neuter a model that is behaving."""
    from app.pricing.comps import SoldDataset
    from app.pricing.pipeline import run as run_pipeline

    sold = _sales(3000, seed=4).rename(columns={
        "sale_price": "price_numeric", "floor_area_m2": "key_floor_area",
        "land_area_m2": "key_land_area", "beds": "key_bedrooms",
        "baths": "key_bathrooms"})
    sold["address"] = [f"{i} Sold St" for i in range(len(sold))]
    sold["land_value_numeric"] = sold["cv_numeric"] * 0.55
    sold["building_age"] = 2005
    live = _sales(120, seed=13).rename(columns={
        "floor_area_m2": "key_floor_area", "land_area_m2": "key_land_area",
        "beds": "key_bedrooms", "baths": "key_bathrooms"}).drop(columns=["sale_price"])
    live["address"] = [f"{i} Live Rd" for i in range(len(live))]
    live["price_numeric"] = None
    live["price_display"] = "Auction"
    live["key_carspaces"] = 2
    live["land_value_numeric"] = None
    live["building_age"] = 2005
    live["has_swimming_pool"] = False

    sd = SoldDataset(sold)
    before = run_pipeline(live.copy(), sd)
    after = run_pipeline(live.copy(), sd, model=fit(sold))
    a = pd.to_numeric(before["fair_value"], errors="coerce")
    b = pd.to_numeric(after["fair_value"], errors="coerce")
    both = a.notna() & b.notna()
    moved = ((b[both] - a[both]).abs() / a[both] > 0.001).sum()
    assert moved > 0, "the bound has switched the model off entirely"


def test_the_bound_is_tighter_than_a_deal():
    """A deal is a listing 10-20% below value. A model free to move a valuation
    30% is a model free to invent and destroy deals wholesale."""
    from app.pricing.pipeline import ML_MAX_SHIFT
    assert ML_MAX_SHIFT <= 0.10


# ---- a listing has no sale date, and that used to mean 'two years ago' ----
def test_an_undated_listing_is_valued_at_todays_market_not_the_oldest_month():
    """A for-sale listing has no sold_date — it has not sold. The time feature
    fell to zero, which is the OLDEST month in the training window, so every
    live listing was priced at a market up to two years stale. In a market that
    has moved, that is one error in one direction on every listing at once."""
    df = _sales(2000, seed=8)
    df["sold_date"] = [f"2026-{(i % 6) + 1:02d}-14" for i in range(len(df))]
    m = fit(df)
    assert m.latest_month > m.base_month, "the fit did not record its newest month"

    listings = df.head(200).drop(columns=["sold_date", "sale_price"])
    newest = df[F.months(df) == F.months(df).max()].head(200).drop(columns=["sale_price"])

    undated = m.predict(listings)
    dated_newest = m.predict(newest)
    # An undated listing must be valued as if it were selling now, so its
    # implied month matches the newest, not the oldest.
    d = F.build(listings, medians=m.medians, base_month=m.base_month,
                training=False, latest_month=m.latest_month)
    col_i = d.names.index("months")
    assert d.X[:, col_i].min() == pytest.approx(m.latest_month - m.base_month)
    assert len(undated) == len(listings)
    assert len(dated_newest) > 0


def test_the_newest_month_survives_serialisation():
    m = fit(_sales(1500, seed=9))
    assert Model.from_json(m.to_json()).latest_month == m.latest_month


# ---- pulling the data out, because guessing is how this happened ----------
def test_the_diagnostic_carries_the_models_raw_opinion(db_session):
    """The column that matters most.

    With the bound in place a broken model looks fine from outside — every
    price within 10% of the previous one — while producing nonsense
    underneath. The raw unbounded prediction is how that is visible BEFORE it
    becomes visible in the prices.
    """
    from app.ml.diagnostic import COLUMNS

    assert "model_prediction" in COLUMNS
    assert "model_vs_published_pct" in COLUMNS
    assert "model_status" in COLUMNS


def test_the_diagnostic_carries_every_pricing_input(db_session):
    """A wrong number has to be traceable to the input that caused it."""
    from app.ml.diagnostic import COLUMNS

    for c in ("cv_numeric", "land_value_numeric", "floor_area_m2",
              "land_area_m2", "beds", "baths", "type_of_title",
              "asking_price", "fair_value", "margin", "is_underpriced"):
        assert c in COLUMNS, c


def test_the_diagnostic_leaks_nothing_personal(db_session):
    """It exists to be SENT to someone for help, so it has to be safe to send."""
    from app.ml.diagnostic import COLUMNS

    banned = ("email", "password", "token", "user", "phone", "name_",
              "referral", "commission", "api_key")
    for c in COLUMNS:
        assert not any(b in c.lower() for b in banned), c


def test_the_diagnostic_is_empty_rather_than_broken_with_no_batch(db_session):
    from app.ml.diagnostic import summary, to_csv

    assert to_csv(db_session).startswith("id,address")
    assert summary(db_session)["listings"] == 0
