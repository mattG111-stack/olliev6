"""A rule written twice is a rule that will eventually disagree with itself.

    "the pipeline treated 491 listings as priced while release held the same
     491 as no real asking"

Nothing was wrong with either of those two definitions. They were both sensible
and they were not the same, and the disagreement was invisible because each
module was internally consistent. That is the failure mode this file guards
against, and it is not hypothetical here — it has happened at least twice.

These tests do not check that a threshold has a particular value. Values change.
They check that each threshold has exactly ONE definition, so when it changes it
changes everywhere at once.
"""
from __future__ import annotations

import glob
import os
import re

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")

import pytest  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def _sources() -> dict[str, str]:
    out = {}
    for path in glob.glob(os.path.join(APP, "**", "*.py"), recursive=True):
        rel = os.path.relpath(path, APP)
        out[rel] = open(path, encoding="utf-8").read()
    return out


def _uses(pattern: str, *, skip: set[str] = frozenset()) -> list[str]:
    """Files containing the pattern, ignoring comments and docstrings well
    enough for this purpose: a line whose first non-space character is # or
    whose match sits inside a quoted string is prose, not a rule."""
    hits = []
    rx = re.compile(pattern)
    for rel, src in _sources().items():
        if rel in skip:
            continue
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') \
                    or stripped.startswith("'"):
                continue
            code = line.split("#", 1)[0]
            if rx.search(code):
                hits.append(f"{rel}: {stripped}")
                break
    return hits


# ---- the CV-vs-asking sanity rule -------------------------------------------
def test_the_broken_cv_ratio_is_defined_once():
    """A council valuation more than 2.5x the asking price is a broken record,
    not a bargain. That was written out in the pricing run, the hedonic and the
    audit, with no name shared between them — three copies of one idea, each
    free to drift."""
    from app.pricing.assumptions import CV_IMPLAUSIBLE_VS_ASKING

    assert CV_IMPLAUSIBLE_VS_ASKING > 1.0
    literal = _uses(r"2\.5\s*\*\s*(cv|asking|r\[)", skip={"pricing/assumptions.py"})
    assert literal == [], (
        "the 2.5x rule is written out again in:\n  " + "\n  ".join(literal)
        + "\nUse assumptions.CV_IMPLAUSIBLE_VS_ASKING."
    )


def test_all_three_callers_read_the_shared_name():
    """The other half — extracting a constant nobody imports achieves nothing.
    ASKING_PRICE_MIN sat in assumptions with zero readers while two modules
    wrote the literal."""
    for mod in ("pricing/pipeline.py", "pricing/glm.py", "pricing/audit.py"):
        assert "CV_IMPLAUSIBLE_VS_ASKING" in _sources()[mod], mod


# ---- the placeholder-asking floor -------------------------------------------
def test_the_minimum_real_asking_price_is_defined_once():
    """$1 and $2 asking prices are a by-negotiation listing with the field
    filled in, not cheap houses."""
    from app.pricing.assumptions import ASKING_PRICE_MIN

    assert ASKING_PRICE_MIN > 0
    literal = _uses(r"asking\s*(is not None and asking\s*)?<\s*10_?000",
                    skip={"pricing/assumptions.py"})
    assert literal == [], (
        "the minimum-asking rule is written out again in:\n  "
        + "\n  ".join(literal) + "\nUse assumptions.ASKING_PRICE_MIN."
    )


def test_it_is_actually_read_where_the_rule_is_applied():
    src = _sources()
    assert "ASKING_PRICE_MIN" in src["ingest.py"]
    assert "ASKING_PRICE_MIN" in src["preflight_file.py"]


# ---- the buy-price discount --------------------------------------------------
def test_the_buy_price_discount_is_defined_once():
    from app.pricing.buyprice import DISCOUNT

    assert 0.0 < DISCOUNT <= 1.0
    literal = _uses(r"0\.95\s*\*\s*float\(asking\)", skip={"pricing/buyprice.py"})
    assert literal == [], (
        "the buy-price discount is written out again in:\n  " + "\n  ".join(literal)
    )


# ---- the one that already bit ------------------------------------------------
def test_the_placeholder_price_rule_has_exactly_one_definition():
    """The original. Three spellings of "is this a real asking price", one per
    module, disagreeing on 491 listings."""
    src = _sources()
    definitions = [rel for rel, s in src.items()
                   if re.search(r"^def is_placeholder_price", s, re.M)]
    assert definitions == ["prior_price.py"], definitions


@pytest.mark.parametrize("mod", ["pricing/pipeline.py", "release.py"])
def test_the_other_callers_import_it_rather_than_restating_it(mod):
    assert "is_placeholder_price" in _sources()[mod]


# ---- which sold rows are "the sold data" -------------------------------------
#
# The second time this exact shape bit. sold_batch_ids() was written to be the
# one answer to "which sold rows count", and then a dozen places went on
# resolving it themselves as "the batch with is_active set". That was right
# while each upload replaced the last. Once history began accumulating, every
# one of them silently narrowed to whichever delivery arrived most recently —
# a few hundred sales out of a hundred thousand.
#
# Nothing errored. The renovation tool came back with no districts, the
# days-to-sell chart drew a short line, and the comparable sales on a property
# page read like a thin suburb. Four different symptoms, one cause, and no way
# to see it from any of them.

# Reading ONE batch is legitimate when the subject IS that batch: a diagnostic
# describing a named delivery, or deleting one.
_ONE_BATCH_IS_THE_POINT = {
    "main.py",          # the self-test probes, which describe a chosen batch
    "ingest.py",        # writing, deleting and joining batches
}


def test_nothing_reads_the_sold_data_as_a_single_batch():
    offenders = _uses(r"PropertySold\.import_batch_id\s*==",
                      skip=_ONE_BATCH_IS_THE_POINT)
    assert offenders == [], (
        "these read the sold data as ONE batch:\n  " + "\n  ".join(offenders)
        + "\nSold history accumulates — a batch is a delivery, not the dataset."
          "\nUse ingest.sold_batch_ids(db, region) and filter with .in_()."
    )


def test_the_assistant_uses_the_shared_helper():
    """Ollie was the last holdout, and the one a customer talks to. Its own
    _active() helper takes no ordering and no region, so "the sold batch" was
    whichever row the database happened to return first."""
    src = _sources()["assistant/tools.py"]
    assert "sold_batch_ids" in src, (
        "the assistant resolves the sold data by itself again")
    assert not re.search(r'_active\(\s*s\s*,\s*["\']sold["\']\s*\)', src), (
        "_active(s, \"sold\") is back in the assistant — it returns one "
        "delivery, unordered")
