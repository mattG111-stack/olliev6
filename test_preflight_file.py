"""Check the file before you load it.

    "i need to be able to download the data before we load it"
    "the data was shit"

A load has been a one-way door. 146 MB in, a count out, and the rejected rows
gone — not stored, not listed, not recoverable. So "why isn't 36 Lloyd Ave in
the system" had no answer at all, and "10,608 rows not in this region" on a file
named hougarden_output_auckland_v2.csv was a fact nobody could act on.

Two things have to be true for this to be worth having.

  IT AGREES WITH THE REAL LOAD. A preflight that says a row will load, and then
  the load drops it, is worse than no preflight, because it is trusted. The rules
  here are ingest's own — imported, not copied — and the order is the load's
  order, so a row refused for two reasons is reported under the one you would fix.

  IT WRITES NOTHING. No batch, no job, no listing. Checking a file must not be a
  decision.
"""
from __future__ import annotations

import csv
import io

import pandas as pd
import pytest

from app.ingest import canonical_region, region_is_unreadable, region_matches
from app.preflight_file import check, verdict


def _rows(data: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _listing(**kw):
    d = dict(address="12 Elliot Street", suburb="Remuera", region="Auckland",
             property_type="House", price_numeric=1_000_000.0,
             cv_numeric=1_050_000.0, key_floor_area=150.0,
             key_land_area=500.0, key_bedrooms=3)
    d.update(kw)
    return d


# ---- the thing that cost 10,608 rows ---------------------------------------
def test_a_lowercase_region_is_not_a_different_region(db_session):
    """The whole reason this exists. `payload["region"] != "Auckland"` threw away
    10,608 of 13,914 rows on one real load — 76% of an Auckland file — because
    the newer export slugs its location columns and _deslug() only touches a
    value that is hyphenated. "auckland" came through lowercase and failed."""
    assert region_matches("auckland", "Auckland")
    assert region_matches("AUCKLAND", "Auckland")
    assert region_matches("auckland-region", "Auckland")
    assert region_matches("Auckland Region", "Auckland")
    assert region_matches("  Auckland  ", "Auckland")


def test_a_chinese_region_name_is_the_same_region(db_session):
    """Hougarden is a Chinese-language site. The name arrives in Chinese."""
    assert region_matches("奥克兰", "Auckland")


def test_a_missing_region_is_not_a_wrong_region(db_session):
    """A blank means the feed did not say, not that it said somewhere else."""
    assert region_matches(None, "Auckland")
    assert region_matches("", "Auckland")


def test_another_region_is_still_refused(db_session):
    """The counterweight, and the point of keeping the check at all: the sold
    data is Auckland-only, so comp-matching a Hamilton listing gives nonsense."""
    assert not region_matches("Waikato", "Auckland")
    assert not region_matches("Wellington", "Auckland")
    assert not region_matches("Canterbury", "Auckland")
    # It has to know the rest of the country by name, or "somewhere else" is a
    # category with six members and everything else falls through it.
    assert not region_matches("Otago", "Auckland")
    assert not region_matches("Manawatu-Whanganui", "Auckland")
    assert not region_matches("Hawke's Bay", "Auckland")
    assert not region_matches("Southland", "Auckland")
    assert not region_matches("Christchurch", "Auckland")


# ---- the second attempt: a name nobody can read is not a different region ----
def test_an_unreadable_region_loads_instead_of_vanishing(db_session):
    """The fix that the alias list was not.

    Widening the list only covers spellings somebody thought of. After the first
    fix shipped, a real Auckland load still refused 10,608 rows — so the rule
    changed shape: a string only counts as "somewhere else" if it reads as a
    region we actually know. Anything else is the same non-answer a blank is,
    and a blank was already accepted for exactly that reason.
    """
    assert region_matches("north-shore-city", "Auckland")
    assert region_matches("region-14", "Auckland")
    assert region_matches("残り", "Auckland")
    assert region_matches("", "Auckland")


def test_the_old_auckland_councils_are_auckland(db_session):
    """Amalgamated in 2010, still all over property feeds, because the titles
    and the sales records still carry them. A North Shore listing is an Auckland
    listing and always was — these are read as Auckland, not merely tolerated."""
    for name in ("North Shore City", "Waitakere", "Manukau City",
                 "Papakura District", "Rodney", "Franklin", "Waiheke Island"):
        assert region_matches(name, "Auckland"), name
        assert not region_is_unreadable(name, "Auckland"), name


# ---- what gets decided with, versus what gets stored -------------------------
def test_a_row_stores_the_region_it_was_loaded_under(db_session):
    """Two different questions, and conflating them made rows invisible.

    trademe.fill does `model.region == region`. A sold row whose file spelled it
    "auckland" matches nothing there — the job reports zero matched and raises
    nothing, so the failure is silent and permanent. That was live on every
    lowercase row before this, and the unreadable ones would have joined them.
    """
    assert canonical_region("auckland", "Auckland") == "Auckland"
    assert canonical_region("auckland-region", "Auckland") == "Auckland"
    assert canonical_region("North Shore City", "Auckland") == "Auckland"
    assert canonical_region("奥克兰", "Auckland") == "Auckland"
    assert canonical_region("Region 14", "Auckland") == "Auckland"
    assert canonical_region(None, "Auckland") == "Auckland"
    assert canonical_region("", "Auckland") == "Auckland"


def test_a_row_from_somewhere_else_keeps_saying_so(db_session):
    """The counterweight. Rewriting a Waikato row's region to Auckland would put
    it inside every Auckland query — which is the bug, not the fix."""
    assert canonical_region("Waikato", "Auckland") == "Waikato"
    assert canonical_region("Otago", "Auckland") == "Otago"


def test_the_decision_still_sees_the_string_the_file_carried(db_session):
    """Normalising must not reach the diagnostics. The run log line naming the
    refused strings is the only thing that made this bug findable, and it reads
    the payload, not the stored row."""
    df = pd.DataFrame([_listing(region="Waikato")])
    (row,) = _rows(check(df, "Auckland")[0])
    assert "Waikato" in row["Why"]


def test_a_row_that_loaded_on_an_unreadable_name_says_so(db_session):
    """Loading it is the right call; loading it silently is not. If this number
    is ever large, the export has changed shape again."""
    df = pd.DataFrame([_listing(region="region-14")])
    data, counts = check(df, "Auckland")
    (row,) = _rows(data)

    assert row["Verdict"] == "loaded"
    # Quoted as the load sees it, not as the file spells it — the un-slugging
    # happens first, and reporting the raw string would send you looking for a
    # value that no longer exists by the time the decision is made.
    assert "Region 14" in row["Why"]
    assert counts["_unreadable_region"] == 1


# ---- the report ------------------------------------------------------------
def test_every_row_of_the_file_comes_back_with_a_verdict(db_session):
    """One line in, one line out. A summary is what the run log already gives,
    and it could not answer "was MY house one of the 10,608"."""
    df = pd.DataFrame([_listing(address=f"{i} Elliot Street") for i in range(25)])
    out = _rows(check(df, "Auckland")[0])
    assert len(out) == 25


def test_a_refused_row_says_why_in_words(db_session):
    df = pd.DataFrame([_listing(address="5 Tower Way", property_type="Apartment")])
    (row,) = _rows(check(df, "Auckland")[0])

    assert row["Verdict"] == "REJECTED"
    assert "apartments" in row["Why"].lower()


def test_the_reason_carries_the_figures_that_caused_it(db_session):
    """"asking more than 50% away from the CV" is a rule. "asking $400,000 is
    more than 50% away from the $1,050,000 council valuation" is a row you can
    go and look at."""
    df = pd.DataFrame([_listing(price_numeric=400_000.0, cv_numeric=1_050_000.0)])
    (row,) = _rows(check(df, "Auckland")[0])

    assert "400,000" in row["Why"] and "1,050,000" in row["Why"]


def test_the_refused_region_string_is_quoted_back(db_session):
    """Naming the actual string is what turns "not in this region" from a
    mystery into a one-line diagnosis."""
    df = pd.DataFrame([_listing(region="Waikato")])
    (row,) = _rows(check(df, "Auckland")[0])

    assert "Waikato" in row["Why"]


def test_a_good_row_is_marked_loaded_with_no_reason(db_session):
    df = pd.DataFrame([_listing()])
    (row,) = _rows(check(df, "Auckland")[0])

    assert row["Verdict"] == "loaded"
    assert row["Why"] == ""


def test_the_counts_match_the_rows(db_session):
    """The header counts are what the page shows. They cannot disagree with the
    file it hands you at the same moment."""
    df = pd.DataFrame([_listing(), _listing(region="Waikato"),
                       _listing(property_type="Apartment")])
    data, counts = check(df, "Auckland")
    out = _rows(data)

    assert counts["_total"] == 3 == len(out)
    assert counts["_loaded"] == sum(1 for r in out if r["Verdict"] == "loaded")


# ---- it must agree with the real load, and change nothing -------------------
def test_the_first_reason_wins_the_same_way_the_load_orders_them(db_session):
    """A row refused for two reasons is reported under the first — the one you
    would fix. Reporting the second sends somebody to fix the wrong thing."""
    row = _listing(region="Waikato", property_type="Apartment", cv_numeric=None)
    code, _ = verdict({"region": "Waikato", "suburb": "Hamilton East",
                       "property_type": "Apartment", "cv_numeric": None,
                       "address": "1 Somewhere Road"}, row, "Auckland")
    assert code == "non_target_region"


def test_checking_a_file_writes_nothing(db_session):
    """Checking must not be a decision. No batch, no job, no listing."""
    from app.models import ImportBatch, IngestJob, PropertyForSale

    before = (db_session.query(ImportBatch).count(),
              db_session.query(IngestJob).count(),
              db_session.query(PropertyForSale).count())

    check(pd.DataFrame([_listing(), _listing(region="Waikato")]), "Auckland")

    assert (db_session.query(ImportBatch).count(),
            db_session.query(IngestJob).count(),
            db_session.query(PropertyForSale).count()) == before


def test_an_empty_file_is_answered_not_crashed(db_session):
    data, counts = check(pd.DataFrame(), "Auckland")
    assert counts["_total"] == 0
    assert _rows(data) == []
