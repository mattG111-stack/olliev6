"""A carried price has to be distinguishable from a price a vendor named.

    "also one other thing if a house is in the last file for sale with a price
     and now it is price by negtion we add that price it was before"
    "and we have was list at $ whatever it was on the listing at this date"
    "are the houses in there that had list prices before"

The carry-forward was built, and then that last question could not be answered
from an export of 9,102 listings — because nothing downstream carried the
answer. The derived figure went into the Asking column looking exactly like an
advertised price, and there was no column anywhere saying which was which.

Two different questions both went unanswerable:

  WHICH OF THESE PRICES DID A VENDOR ACTUALLY NAME. A carried price is our
  inference, a good one, and it is not the same kind of fact as an asking price.
  Anyone reviewing a batch before it goes live needs to see the difference.

  DID THE CARRY-FORWARD EVEN RUN. With nothing recorded on the row, a week where
  it rescued nothing looks identical to a week where it never executed.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")

from datetime import datetime, timezone  # noqa: E402

from app.models import PropertyForSale  # noqa: E402
from app.prior_price import ADVERTISED_BASIS, DERIVED_BASIS  # noqa: E402
from app.routers.release import StagedGridRow, _grid_row  # noqa: E402


def _row(**kw) -> PropertyForSale:
    base = dict(id=1, address="150 The Drive", suburb="Epsom", property_type="House",
                asking_price=2_500_000.0, cv_numeric=3_275_000.0,
                fair_value=3_144_077.0, margin=0.257)
    base.update(kw)
    return PropertyForSale(**base)


def test_the_grid_says_where_the_asking_price_came_from():
    r = _grid_row(_row(asking_basis=DERIVED_BASIS,
                       prior_asking_price=2_580_000.0,
                       prior_asking_seen_at=datetime(2026, 8, 18,
                                                     tzinfo=timezone.utc)))
    assert r.asking_basis == DERIVED_BASIS
    assert r.prior_asking_price == 2_580_000.0
    assert r.prior_asking_seen_at.startswith("2026-08-18")


def test_an_advertised_price_says_so_too():
    """Both sides named. "Blank means advertised" is a convention somebody has
    to be told, and an export is read by people who were not."""
    r = _grid_row(_row(asking_basis=ADVERTISED_BASIS))
    assert r.asking_basis == ADVERTISED_BASIS
    assert r.prior_asking_price is None


def test_a_row_from_before_any_of_this_does_not_break_the_grid():
    """Batches loaded before these columns existed are still live."""
    r = _grid_row(_row())
    assert r.asking_basis is None
    assert r.prior_asking_price is None
    assert r.prior_asking_seen_at is None


def test_the_three_fields_are_on_the_model_the_page_reads():
    """The bug was never in the carry-forward — it was that nothing downstream
    carried the answer. This is the part that was missing."""
    for f in ("asking_basis", "prior_asking_price", "prior_asking_seen_at"):
        assert f in StagedGridRow.model_fields, f


def test_the_derived_basis_names_the_discount_rather_than_hiding_it():
    """"last advertised, less 3%" is a sentence somebody can check. "derived" is
    a word that needs the code open beside it."""
    assert "%" in DERIVED_BASIS
    assert "advertised" in DERIVED_BASIS
