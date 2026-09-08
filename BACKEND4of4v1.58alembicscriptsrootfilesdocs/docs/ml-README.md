# The valuation, fitted on our own sales

## What was here before

Nothing. There was no machine learning in this product.

The valuation ran a hedonic GLM whose coefficients were extracted from
`Algo data 17-05-2026.xlsx` — a client spreadsheet — into `app/pricing/v38_tables/`.
They were fitted once, elsewhere, on data we do not hold. There was no training
script anywhere in the repository, and no ML library in `requirements.txt`.

Sold files have landed every week since May. Not one of them moved a single
coefficient. A fixed coefficient is a claim that the market has not changed,
restated confidently on every page load.

`CompEngine` in `app/pricing/buyprice.py` computes medians and sale/CV ratios at
request time. That is useful and it is statistics; it is not a model that learns.

## What this is

Four files:

| file | what it does |
|---|---|
| `features.py` | turns a table of sales into a matrix — missing values kept as signal, areas logged, time as a column |
| `train.py` | fits it: ridge regression on log price, plus shrunk suburb/type residual effects |
| `evaluate.py` | scores it against raw council CV **and** the estimator running today, on sales it never saw |
| `store.py` | keeps every fit with the numbers that judged it; decides which one is live |

## Why ridge and not a gradient-boosted forest

Three reasons, in order of weight.

**It is the same functional form the v3.5 GLM already uses.** That makes this a
like-for-like swap — same features, same shape, our data instead of a
spreadsheet's — and the backtest can settle it cleanly. A forest would change two
things at once and leave it unclear which one moved the number.

**No new dependency.** scikit-learn and its dependencies are a large addition to
a container that has been OOM-killed twice and already carries 127 MB before a
model is loaded. This fit is a linear system; numpy solves it.

**The ceiling here is data, not model class.** The strongest coefficient is
`ln_cv` at +0.96 — a sale is mostly its council valuation, and the rest is
suburb. A more flexible model has little left to find until there are more
features. Bedroom and bathroom counts are the obvious next ones; the committed
test fixture carries neither.

If those land and there is still headroom, boosting is the next thing to try.

## How it is judged

Two splits, because they answer different questions.

A **random split** asks "does this generalise". A **forward split** — fit on
everything before a date, test on everything after — asks "would this have worked
in production", which is the only way it is ever used. A model can pass the first
and fail the second when it has learned a market that has since moved, and that
failure is invisible to a random split because the future is scattered through
the training set.

The forward split gates shipping. `should_ship` requires all three:

1. it beats raw council CV — otherwise it is worse than free
2. it beats the live engine by at least 0.15 percentage points
3. at least 200 held-out sales, so the median means something

A model that fails is stored anyway, marked failed, with the sentence saying why.
Three failures in a row is how you learn the incoming data has a problem, and
that signal is lost if failures are thrown away.

## Measured

10,168 real Auckland sales (`tests/fixtures/sold_comps.csv`), median % error,
forward split at five cut points:

| train up to | test n | trained model | live engine | raw CV |
|---|---|---|---|---|
| Sep 2025 | 3,958 | **7.65%** | 7.72% | 7.78% |
| Nov 2025 | 2,840 | **7.57%** | 8.01% | 8.00% |
| Dec 2025 | 2,402 | **7.66%** | 7.86% | 7.85% |
| Feb 2026 | 1,722 | **7.64%** | 7.89% | 7.95% |
| Mar 2026 | 1,196 | **7.45%** | 7.90% | 8.01% |
| **mean** | | **7.59%** | 7.88% | 7.92% |

Beat the live engine at 5/5 cut points, mean +0.28 points; +0.32 against CV.

**On random splits it is a tie** — 6.95% vs 6.99%. That is the finding, not a
footnote. The advantage is specifically at predicting *forward*, because the
trained model carries a time trend and current suburb effects while the
spreadsheet is frozen in May 2026. The advantage should grow as the spreadsheet
ages, and it would shrink to nothing if someone re-extracted a fresh one.

Read the size honestly: 0.28 points is a real, repeatable improvement and it is
not a transformation. The transformation would need better inputs.

Things tried that did **not** help, measured across five splits rather than
cherry-picked from one:

| feature | mean delta | splits won |
|---|---|---|
| floor/land ratio | −0.006 pts | 3/5 |
| ln(land)² | ~0 | — |
| ln(CV)² | worse | — |
| CV per floor m² | ~0 | — |
| months² | worse | — |

All noise. The base feature set stands.

## It turns itself on

A model that passes the gate becomes live immediately. No button.

This started as an opt-in switch defaulting to off, and that was wrong. The gate
already refuses anything that does not beat both the council figure and the
estimator running today, on sales it never saw, on a forward split. Requiring a
human to then agree adds no safety — the measurement *is* the decision — it just
means a valuation measured as better sits unused until somebody remembers.

`ml.valuation_enabled` is now an **off override**: absent means use it, only an
explicit `0` stops it. What you keep is the ability to stop it instantly and to
roll back to any earlier fit, which is the part that actually matters.

Admin → Data → **Valuation model**: fit, read the three numbers, re-price to
apply it to live listings. Every fit is listed, including the ones that did not
pass and changed nothing.

## Where it plugs in

`pipeline.run(..., model=...)`, substituted at `ollie_value` — the published
valuation, which is CV multiplied by the area's shrunk sale/CV ratio.

**Not** in `glm.predict`. That was the first attempt and it did nothing at all:
the pipeline logged "trained valuation priced 40 of 40 rows" and every published
price came back byte-identical, because the v3.5 hedonic feeds `market_value`
and the buy price rather than the number a customer reads as the valuation.
`test_the_model_actually_changes_a_published_price` exists so that cannot happen
again — "the model is loaded" and "the model is used" are different claims.

The substitution sits **before** every cap below it, so a trained value inherits
the size-aware floor-rate cap, the bed-count cap and the last-sold cap. Those
caps exist because a valuation occasionally comes back wrong in a way that
reaches a customer; a trained model is a different way of producing the same
number, not an exemption from the checks on it.

## What could go wrong, and what stops it

| risk | control |
|---|---|
| a sale helps predict itself | suburb effects fitted on training rows only; `test_suburb_effects_come_from_training_rows_only` |
| broken records train the model | sale/CV outside 0.3–3.0 excluded before fitting |
| a thin suburb fits noise | shrinkage, `K_SUBURB = 30` |
| the model quotes 8× CV | prediction clamped to 0.35–3.0 × CV |
| a worse model ships | `should_ship`, three conditions, all on the forward split |
| the model vanishes on redeploy | serialised to `trained_models`, loaded on demand |
| a corrupt payload takes the site down | `load()` returns `None` and the previous valuation runs |
