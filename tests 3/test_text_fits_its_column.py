"""A sentence one character too long is a failed load, not a shortened sentence.

Postgres does not truncate an overlong value into a VARCHAR — it refuses the
write, and the refusal takes the transaction with it. So the cost of a reworded
status line is not a clipped status line; it is a stage that reports as broken,
or a 146 MB import that dies partway through with a database error naming a
column rather than a listing.

This has already happened once here, to IngestJob.stage. These tests are the
thing that was missing: they pin every piece of composed text against the width
of the column it is going into, so the next rewording fails in CI instead of in
production. Nothing here checks wording. It checks that the wording fits.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")

import pytest  # noqa: E402

from app.models import IngestJob, PropertyForSale, RunEvent  # noqa: E402


def _width(model, name: str) -> int:
    col = model.__table__.columns[name]
    n = getattr(col.type, "length", None)
    assert n, f"{name} is not a bounded text column any more"
    return n


# ---- the reasons a deal is refused ------------------------------------------
# Written with the biggest numbers the format can produce. A nine-figure price
# is not realistic; the point is that the headroom does not depend on nobody
# ever loading a mansion.
_BIG = 999_999_999.0
_COMPS = 8


@pytest.mark.parametrize("reason", [
    # The six that need no numbers to be long.
    "Premium home — worth is withheld at this end of the market",

    # listing_type is a closed set — fixed | auction | tender | negotiation |
    # unknown — so the longest it can be is "negotiation".
    "No advertised price (negotiation) — the figure in the feed is a search "
    "price, not what the vendor is asking, and no earlier load has a price to "
    "carry",

    "The listed price is the council valuation, not a real asking price",
    "Guide price (“from $X”) — the real price is higher",
    "No floor area recorded, so the valuation cannot be size-checked against "
    "comparable homes",
    "Not enough comparable sales nearby to value it with enough confidence "
    "to publish",

    # days-on-market as a five-figure number, which is longer than any listing
    # has ever been on the market and still has to fit.
    "On the market 99999 days — the market has already priced it, so the gap "
    "is not a discount",

    "Valued from comparable sales rather than the council record — good "
    "enough to publish a value, not good enough to call a discount",

    # And the four built from prices.
    f"Council valuation ${_BIG:,.0f} is more than double the ${_BIG:,.0f} "
    f"asking — one of the two is wrong and there is no way to tell which",

    f"Council record values the land only — the gap to the ${_BIG:,.0f} asking "
    f"is a house against dirt, not a discount",

    f"Asking ${_BIG:,.0f} is 100% of the ${_BIG:,.0f} council valuation and "
    f"only 999 sold comps back the valuation — {_COMPS}+ at high confidence "
    f"would carry it",

    f"Valuation ${_BIG:,.0f} is 99.9x the ${_BIG:,.0f} asking — not a find, "
    f"an input is wrong",
])
def test_a_blocked_deal_reason_fits_its_column(reason):
    assert len(reason) <= _width(PropertyForSale, "deal_block_reason"), (
        f"{len(reason)} chars into a "
        f"{_width(PropertyForSale, 'deal_block_reason')}-char column"
    )


def test_the_reasons_are_actually_the_ones_in_the_pricing_code():
    """The test above is worthless if the strings drift apart from the real
    ones. This does not compare wording — it checks that every f-string
    assigned to deal_block reads from the same small set of shapes, so a new
    reason that is not covered here shows up as a count mismatch."""
    import inspect
    import re

    from app.pricing import pipeline

    src = inspect.getsource(pipeline.run)
    # Assignments of an actual message, not `deal_block = None`.
    assigned = [m for m in re.findall(r"deal_block = (\S+)", src) if m != "None"]
    assert len(assigned) == 11, (
        f"{len(assigned)} places set a deal_block message; the length tests "
        f"above cover 11. Add the new one to them — a reason that does not fit "
        f"its column does not get shortened, it fails the write."
    )


# ---- the stage line on a job row --------------------------------------------
def test_the_longest_stage_line_fits():
    for line in ("rate-limited, waiting 3600s",
                 "carrying prices forward",
                 "done", "error", "enrich", "price"):
        assert len(line) <= _width(IngestJob, "stage")


def test_an_overlong_stage_line_is_trimmed_rather_than_refused(monkeypatch):
    """The guard, not the measurement. _update trims on the way in so a caller
    that composes something long gets a clipped status line instead of a stage
    that reports as broken."""
    from app import staged_stages

    captured: dict = {}

    class _Q:
        def filter(self, *a, **k):
            return self

        def update(self, kwargs):
            captured.update(kwargs)

    class _DB:
        def query(self, *a, **k):
            return _Q()

        def commit(self):
            pass

    staged_stages._update(_DB(), 1, stage="x" * 400, status="running")

    assert len(captured["stage"]) == _width(IngestJob, "stage")
    assert captured["status"] == "running"


# ---- the run log ------------------------------------------------------------
def test_the_run_log_clamps_every_bounded_field():
    """record() already trims. This pins the numbers it trims to against the
    columns, because the two were written in different places."""
    import inspect

    from app import runlog

    src = inspect.getsource(runlog.record)
    for field, model in (("stage", RunEvent), ("event", RunEvent),
                         ("level", RunEvent), ("address", RunEvent)):
        assert f"[:{_width(model, field)}]" in src, (
            f"record() does not trim {field} to {_width(model, field)}"
        )


# ---- values that come off the file ------------------------------------------
def test_a_long_address_is_trimmed_to_fit():
    """Not composed by us — read off a scrape, where a bad parse can produce a
    whole paragraph in the address column."""
    from app.ingest import _common_property_payload

    payload = _common_property_payload({"address": "x" * 5_000,
                                        "suburb": "y" * 5_000,
                                        "property_type": "z" * 5_000})

    assert len(payload["address"]) == _width(PropertyForSale, "address")
    assert len(payload["suburb"]) == _width(PropertyForSale, "suburb")
    assert len(payload["property_type"]) == _width(PropertyForSale,
                                                   "property_type")


def test_an_ordinary_address_is_left_exactly_as_it_was():
    from app.ingest import _common_property_payload

    payload = _common_property_payload({"address": "12 Elliot Street",
                                        "suburb": "Remuera"})
    assert payload["address"] == "12 Elliot Street"
    assert payload["suburb"] == "Remuera"
