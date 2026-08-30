"""Ours vs Hougarden vs the council figure, by property type.

    "can we have it on the dashboard our mape number vs hougarden which is the
     for sale data"  /  "mape on house, townhouse, apartment"

The comparison is easy to do dishonestly and the dishonest version looks better,
so most of these tests are about refusing rather than computing.

THREE WAYS THIS GOES WRONG IF NOBODY IS WATCHING

Comparing two estimates instead of measuring against a sale. Two valuations of a
house that has not sold can disagree by 20% and neither is wrong yet. Only the
price it actually went for settles anything.

Scoring ourselves on a sale we have already seen. There is no "our valuation"
column on a sold record, and if there were it would have been written after the
sale with the sale in its own comp set. It would score beautifully and mean
nothing. Ours is fitted forward, on sales that came BEFORE each one.

Scoring the three on different rows. Hougarden does not estimate everything.
Ourselves on 40,000 sales and them on the 12,000 they cover is two different
markets — and it flatters us, because the properties a portal will commit to a
number on are the ordinary well-traded ones every method prices well.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.ml import accuracy as A


def _sold(n=2500, seed=0, hg_coverage=1.0, hg_error=0.11, months=12):
    """Sales with a council valuation and a portal estimate of known quality."""
    rng = np.random.default_rng(seed)
    subs = ["Riverhead", "Glenfield", "Papakura", "Mount Albert"]
    types = rng.choice(["House", "Townhouse", "Apartment"], n, p=[0.7, 0.2, 0.1])
    cv = rng.lognormal(14.0, 0.35, n)
    floor = rng.normal(190, 45, n).clip(60, 500)
    beds = rng.integers(2, 6, n)
    price = cv * np.exp(rng.normal(0.03, 0.08, n))

    hg = cv * np.exp(rng.normal(0.01, hg_error, n))
    if hg_coverage < 1.0:
        hg[rng.random(n) > hg_coverage] = np.nan

    return pd.DataFrame({
        "suburb": rng.choice(subs, n),
        "district": "Rodney",
        "property_type": types,
        "type_of_title": "1.0",
        "sale_price": price,
        "cv_numeric": cv,
        "third_party_valuation": hg,
        "floor_area_m2": floor,
        "land_area_m2": rng.normal(700, 150, n).clip(200, 2000),
        "beds": beds,
        "baths": rng.integers(1, 4, n),
        "sold_date": [f"2026-{(i % months) + 1:02d}-14" for i in range(n)],
    })


# ---- it produces the three numbers, split by type -------------------------
def test_it_reports_all_three_methods_by_property_type():
    r = A.compare(_sold())
    assert r["reason"] is None, r["reason"]
    got = {row["property_type"] for row in r["rows"]}
    assert {"House", "Townhouse", "Apartment"} & got
    for row in r["rows"]:
        for method in ("ours", "hougarden", "council"):
            assert method in row


def test_there_is_an_overall_row_as_well_as_the_split():
    r = A.compare(_sold())
    assert r["overall"]["property_type"] == "All"
    assert r["overall"]["n"] >= max(row["n"] for row in r["rows"])


def test_it_reports_the_median_beside_the_mape():
    """MAPE is what people ask for and it is badly behaved on property: one sale
    recorded at a tenth of its real price moves the mean of 500 by a point."""
    r = A.compare(_sold())
    c = r["overall"]["ours"]
    assert c["mape"] is not None and c["median"] is not None
    assert c["within_10"] is not None


def test_a_worse_portal_estimate_scores_worse():
    """The comparison has to be able to tell them apart at all."""
    good = A.compare(_sold(hg_error=0.05, seed=1))["overall"]["hougarden"]["mape"]
    bad = A.compare(_sold(hg_error=0.25, seed=1))["overall"]["hougarden"]["mape"]
    assert bad > good


# ---- the fairness rules ----------------------------------------------------
def test_all_three_are_scored_on_exactly_the_same_properties():
    """The rule that stops the comparison being a brochure.

    With Hougarden covering only 40% of sales, every method must be measured on
    that 40% — not us on everything and them on their own subset.
    """
    r = A.compare(_sold(hg_coverage=0.4, seed=2))
    assert r["reason"] is None
    o = r["overall"]
    assert o["ours"]["n"] == o["hougarden"]["n"] == o["council"]["n"], o


def test_our_figure_never_sees_the_sale_it_is_scored_on():
    """Fitted on the earlier sales, scored on the later ones. If ours were
    fitted on everything it would score far better than either alternative and
    the number would be a fiction."""
    df = _sold(3000, seed=3)
    r = A.compare(df)
    assert r["trained_on"] and r["trained_on"] < len(df)
    assert r["tested_from_month"] is not None
    # And the tested rows are strictly after the training cut.
    assert r["overall"]["n"] + r["trained_on"] <= len(df)


def test_it_refuses_when_there_is_no_hougarden_estimate_at_all():
    """The committed fixture of 10,168 real sales carries no Hougarden column.
    Showing a two-way comparison labelled as a three-way one, or quietly
    dropping their column, would both be worse than saying so."""
    df = _sold()
    df["third_party_valuation"] = np.nan
    r = A.compare(df)
    assert r["rows"] == []
    assert "Hougarden" in r["reason"]


def test_it_refuses_when_the_sales_carry_no_dates():
    """No dates means no forward split, which means our number would be scored
    on sales it had already learned from."""
    df = _sold()
    df["sold_date"] = None
    r = A.compare(df)
    assert r["rows"] == []
    assert "flatter" in r["reason"]


def test_it_refuses_on_too_little_data_rather_than_guessing():
    r = A.compare(_sold(60))
    assert r["rows"] == []
    assert r["reason"]


def test_broken_records_are_excluded_before_anything_is_measured():
    """A sale at 8x its CV is a family transfer or a mis-keyed record. Left in,
    it rewards whichever method happens to sit nearest a wrong number."""
    df = _sold(2500, seed=4)
    clean = A.compare(df)["overall"]["n"]
    df.loc[df.index[:200], "sale_price"] = df["cv_numeric"].iloc[:200] * 8
    dirty = A.compare(df)["overall"]["n"]
    assert dirty < clean


# ---- thin cells say so rather than showing a number -----------------------
def test_a_thin_property_type_reports_no_number():
    """A percentage error off eleven apartments is one building's story."""
    df = _sold(2500, seed=5)
    df.loc[df["property_type"] == "Apartment", "property_type"] = "House"
    df.loc[df.index[:11], "property_type"] = "Apartment"
    r = A.compare(df)
    apt = [row for row in r["rows"] if row["property_type"] == "Apartment"]
    if apt:
        assert apt[0]["ours"]["mape"] is None
        assert apt[0]["ours"]["n"] < A.MIN_ROWS


def test_the_thin_threshold_is_stated_so_the_page_can_explain_itself():
    r = A.compare(_sold())
    assert r["min_rows"] == A.MIN_ROWS


# ---- the portal's range is used when they publish no point estimate -------
def test_the_midpoint_of_their_range_is_used_when_there_is_no_point_estimate():
    df = _sold(2500, seed=6)
    df["third_party_valuation_low"] = df["third_party_valuation"] * 0.95
    df["third_party_valuation_high"] = df["third_party_valuation"] * 1.05
    df["third_party_valuation"] = np.nan
    r = A.compare(df)
    assert r["reason"] is None
    assert r["overall"]["hougarden"]["mape"] is not None


def test_a_point_estimate_wins_over_the_range():
    """Their published number is the one to hold them to; the range midpoint is
    the fallback for listings where they will not commit to a point."""
    df = _sold(2500, seed=7)
    df["third_party_valuation_low"] = 1.0            # absurd, must not be used
    df["third_party_valuation_high"] = 2.0
    r = A.compare(df)
    assert r["overall"]["hougarden"]["mape"] < 100


# ---- and it says how it was measured --------------------------------------
def test_it_carries_the_method_in_words():
    """A number like this gets screenshotted. It travels with its method."""
    r = A.compare(_sold())
    m = r["method"].lower()
    assert "actually sold" in m
    assert "before" in m
    assert "same properties" in m
