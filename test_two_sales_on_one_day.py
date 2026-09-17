"""Two houses that sold on the same day are two sales.

    "number one get the data right"

The sold history is the foundation. Every valuation on the site is measured
against it, so a sale that never makes it in is not a missing row — it is
evidence removed from every number that would have used it.

A sale was identified by (slug_id, sold_date), with an empty string when there
was no slug. So every slug-less sale on a given date shared one key and all but
the first were discarded as duplicates of each other. They are not duplicates:
they are different houses that happened to settle on the same day, which is not
a coincidence — sale dates cluster hard on month ends and settlement days.

Measured: three slug-less sales sharing a date, ONE survived.

What made it invisible is that the symptom is silence. The load reports them as
"already held", the comp counts come out low, confidence drops, more rows get
held for thin evidence — and every one of those looks like a data-supply
problem rather than a line of code throwing the data away.

A property is identified by its address perfectly well, so that is the fallback.
And a row with neither a slug nor an address is KEPT: it cannot be shown to be a
duplicate of anything, and a possible double is better than a certain loss — a
duplicate drags the median toward one number, a dropped sale removes the
evidence entirely.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app import ingest as I
from app.models import PropertySold


def _sale(address, price, *, suburb="Glenfield", date="2026-05-01", slug=None,
          floor=150.0, beds=3):
    row = dict(address=address, suburb=suburb, price_numeric=price,
               cv_numeric=price * 1.05, key_floor_area=floor, key_bedrooms=beds,
               key_bathrooms=2, property_type="House", sold_listing_date=date)
    if slug is not None:
        row["slug_id"] = slug
    return row


def _load(db, rows, filename="sold.csv"):
    return I.ingest_sold(db, pd.DataFrame(rows), region="Auckland",
                         filename=filename, append=True)


def _kept(db):
    return sorted(r.address for r in db.query(PropertySold).all())


# ---- the bug ----------------------------------------------------------------
def test_three_houses_that_sold_on_one_day_are_three_sales(db_session):
    """THE BUG. One survived out of three."""
    _load(db_session, [_sale("1 Alpha Road", 900_000.0),
                       _sale("2 Beta Road", 1_100_000.0, floor=170.0, beds=4),
                       _sale("3 Gamma Road", 800_000.0, floor=130.0)])

    assert _kept(db_session) == ["1 Alpha Road", "2 Beta Road", "3 Gamma Road"], (
        "sales were discarded as duplicates of each other for sharing a date")


def test_a_whole_settlement_day_survives(db_session):
    """Sale dates cluster on month ends. A month-end file is the worst case and
    the most common one."""
    rows = [_sale(f"{i} Month End Way", 800_000.0 + i * 1000) for i in range(30)]
    _load(db_session, rows)

    assert db_session.query(PropertySold).count() == 30


# ---- what the dedup is actually for ----------------------------------------
def test_the_same_file_loaded_twice_is_still_one_set_of_sales(db_session):
    """The counterweight, and the reason the check exists: re-uploading a file,
    or two date-ranged exports that overlap at the join, would otherwise double
    every comp in the overlap and drag valuations toward whatever sold twice."""
    rows = [_sale("1 Alpha Road", 900_000.0), _sale("2 Beta Road", 1_100_000.0)]
    _load(db_session, rows)
    _load(db_session, [dict(r) for r in rows], filename="sold-again.csv")

    assert _kept(db_session) == ["1 Alpha Road", "2 Beta Road"]


def test_the_same_house_selling_twice_is_two_sales(db_session):
    """A property really can sell twice — a flip, or a resale a year later. The
    date is half the identity for exactly this reason."""
    _load(db_session, [_sale("1 Alpha Road", 800_000.0, date="2025-03-14"),
                       _sale("1 Alpha Road", 950_000.0, date="2026-05-01")])

    assert db_session.query(PropertySold).count() == 2


def test_a_slug_still_identifies_a_sale_when_the_address_differs(db_session):
    """The same sale written with the address spelled two ways is one sale when
    the slug agrees. The slug is preferred precisely because it survives that."""
    _load(db_session, [_sale("1 Alpha Road", 900_000.0, slug="abc-123")])
    _load(db_session, [_sale("1 Alpha Rd", 900_000.0, slug="abc-123")],
          filename="second.csv")

    assert db_session.query(PropertySold).count() == 1


def test_two_houses_of_the_same_number_in_different_suburbs_are_two_sales(db_session):
    """"12 Queen Street" is not one property. The suburb is part of the address
    key for this reason."""
    _load(db_session, [_sale("12 Queen Street", 900_000.0, suburb="Glenfield"),
                       _sale("12 Queen Street", 1_400_000.0, suburb="Remuera")])

    assert db_session.query(PropertySold).count() == 2


# ---- the rule, on its own ---------------------------------------------------
def test_a_row_we_cannot_identify_is_kept_rather_than_dropped(db_session):
    """No slug and no address. It cannot be shown to be a duplicate of
    anything, and a possible double is better than a certain loss."""
    assert I.sale_identity(None, None, None, "2026-05-01") is None
    assert I.sale_identity("", "   ", "Glenfield", "2026-05-01") is None


def test_the_identity_is_the_property_and_the_date():
    a = I.sale_identity(None, "1 Alpha Road", "Glenfield", "2026-05-01")
    b = I.sale_identity(None, "2 Beta Road", "Glenfield", "2026-05-01")
    c = I.sale_identity(None, "1 Alpha Road", "Glenfield", "2025-03-14")
    assert a != b, "two houses on one day came out identical"
    assert a != c, "one house on two days came out identical"
    assert a == I.sale_identity(None, "1 Alpha Rd", "Glenfield", "2026-05-01"), \
        "the same house written two ways came out different"


# ---- and the same fault one layer up ----------------------------------------
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_slug_column_does_not_collapse_the_file(blank):
    """The in-file dedup sets aside rows with no slug — but "no slug" was
    isna(), which catches None and NaN and nothing else. A CSV writes an EMPTY
    STRING, so a sold file whose slug column is blank rather than absent had
    every row sharing the slug "", and three houses that settled on the same
    day deduped down to one before the cross-batch check ever ran.

    Measured at 3 in, 1 out. This is the likelier of the two in practice,
    because a blank column is what an export produces."""
    rows = [dict(address=a, slug_id=blank, sold_listing_date="2026-05-01",
                 price_numeric=900_000.0)
            for a in ("1 Alpha Road", "2 Beta Road", "3 Gamma Road")]
    out, dropped = I._dedupe_by_slug(pd.DataFrame(rows), keep_repeat_sales=True)

    assert len(out) == 3, f"{dropped} genuine sales dropped as duplicates"


def test_a_real_duplicate_in_the_file_is_still_dropped():
    """The counterweight. The same scrape twice in one file IS one sale, and a
    fix that kept both would double a comp."""
    rows = [dict(address="1 Alpha Road", slug_id="abc", sold_listing_date="2026-05-01",
                 price_numeric=900_000.0),
            dict(address="1 Alpha Road", slug_id="abc", sold_listing_date="2026-05-01",
                 price_numeric=900_000.0),
            dict(address="2 Beta Road", slug_id="def", sold_listing_date="2026-05-01",
                 price_numeric=1_100_000.0)]
    out, dropped = I._dedupe_by_slug(pd.DataFrame(rows), keep_repeat_sales=True)

    assert len(out) == 2 and dropped == 1


def test_the_same_house_selling_twice_survives_the_in_file_dedup():
    """A house that sold in 2020 and again in 2025 is two sales and two comps,
    in one file as much as across two."""
    rows = [dict(address="1 Alpha Road", slug_id="abc",
                 sold_listing_date="2020-02-11", price_numeric=600_000.0),
            dict(address="1 Alpha Road", slug_id="abc",
                 sold_listing_date="2025-09-30", price_numeric=950_000.0)]
    out, _ = I._dedupe_by_slug(pd.DataFrame(rows), keep_repeat_sales=True)

    assert len(out) == 2
