"""The region fix, run through an actual load rather than asserted about.

    "still only 9 under priced houses showing what the fuck"
    "10,608 rows rejected — not in this region"

The first region fix had unit tests. region_matches("auckland", "Auckland") was
True in every one of them, and the next real load still refused 10,608 rows of
an Auckland file. The tests were not wrong; they were testing the wrong layer.
Whether a row survives a load depends on the load, and nothing exercised that.

So these put frames through ingest_for_sale and count what comes out the other
end. They are slower than unit tests and they are the only ones that can answer
the question that was actually asked.

Three things have to hold at once, and the first fix got one of them:

  A ROW THAT NAMES THIS REGION, HOWEVER IT SPELLS IT, LOADS.
  A ROW THAT NAMES A DIFFERENT REGION DOES NOT. The sold data is Auckland-only.
  A ROW THAT LOADS STORES THIS REGION, not the file's spelling of it — or it is
    invisible to every query that compares region to "Auckland".
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.models import ImportBatch, PropertyForSale


def _sold_frame() -> pd.DataFrame:
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Testville",
        "district": "Testville", "property_type": "House",
        "key_bedrooms": 3, "key_bathrooms": 1,
        "key_floor_area": f"{140 + i} sqm", "key_land_area": f"{600 + i} sqm",
        "cv_numeric": 900_000, "price_numeric": 950_000 + i * 1_000,
        "sale_price": 950_000 + i * 1_000, "land_value_numeric": 500_000,
        "improvement_value_numeric": 400_000, "type_of_title": "Freehold",
        "sold_date": "2025-06-01", "region": "Auckland",
    } for i in range(10)])


def _listing(region, i=0) -> dict:
    return {
        "address": f"{i} Example Road", "suburb": "Testville",
        "district": "Testville", "region": region, "property_type": "House",
        "cv_numeric": 900_000, "price_numeric": 900_000,
        "key_floor_area": 140, "key_land_area": 600, "key_bedrooms": 3,
        "key_bathrooms": 1, "type_of_title": "Freehold",
        "land_value_numeric": 500_000, "improvement_value_numeric": 400_000,
    }


def _load(db, regions: list) -> tuple:
    from app.ingest import ingest_for_sale

    frame = pd.DataFrame([_listing(r, i) for i, r in enumerate(regions)])
    res = ingest_for_sale(db, frame, _sold_frame(), "auckland_v2.csv",
                          region="Auckland", publish=True)
    rows = (db.query(PropertyForSale)
            .join(ImportBatch, PropertyForSale.import_batch_id == ImportBatch.id)
            .filter(ImportBatch.batch_type == "for_sale").all())
    return res, rows


# ---- the 10,608 ------------------------------------------------------------
@pytest.mark.parametrize("spelling", [
    # THE STRING. Read out of the customer's own export: 18 of 21 rows say
    # "Auckland Great Area" and 3 say "Auckland". Against the original rule that
    # file loads 3 rows and refuses 18 — 86%, which is the 10,608.
    "Auckland Great Area",
    # The same shape, which is why this is fixed by stripping the decorating
    # words rather than by adding one more alias. None of these was on any list.
    "Greater Auckland", "Auckland Council", "Auckland City Area",
    "Auckland Metro", "auckland great area",

    "Auckland", "auckland", "AUCKLAND", "auckland-region", "Auckland Region",
    "  Auckland  ", "奥克兰", "akl",
    # The councils Auckland was amalgamated from, which the feeds still carry.
    "North Shore City", "waitakere-city", "Manukau City", "Papakura District",
    "Rodney", "Franklin", "Waiheke Island",
    # And a blank, which means the feed did not say.
    None, "",
])
def test_a_row_that_is_auckland_survives_the_load(db_session, spelling):
    _res, rows = _load(db_session, [spelling])
    assert len(rows) == 1, f"{spelling!r} was thrown out of an Auckland load"


@pytest.mark.parametrize("spelling", [
    "region-14", "Zone 7", "???", "残り", "Unknown District",
])
def test_a_region_nobody_can_read_is_not_a_reason_to_bin_a_house(db_session,
                                                                 spelling):
    """The second fix. Widening the list of spellings only ever covers the ones
    somebody thought of; an unreadable string is the same non-answer a blank is."""
    _res, rows = _load(db_session, [spelling])
    assert len(rows) == 1, f"{spelling!r} was thrown out of an Auckland load"


# ---- the counterweight ------------------------------------------------------
@pytest.mark.parametrize("elsewhere", [
    "Waikato", "Wellington", "Canterbury", "Otago", "Southland",
    "Hawke's Bay", "Manawatu-Whanganui", "Christchurch", "Hamilton",
])
def test_a_row_from_somewhere_else_is_still_refused(db_session, elsewhere):
    """Still the point of the check: the sold data is Auckland-only, so
    comp-matching a Hamilton listing produces a confident number about nothing."""
    _res, rows = _load(db_session, [elsewhere])
    assert rows == [], f"{elsewhere!r} loaded into an Auckland batch"


# ---- what gets stored -------------------------------------------------------
@pytest.mark.parametrize("spelling", ["auckland", "North Shore City",
                                      "auckland-region", "region-14", None])
def test_a_loaded_row_stores_the_region_it_was_loaded_under(db_session, spelling):
    """trademe.fill does `model.region == region`. A row filed under "auckland"
    matches nothing there — no error, no log line, just zero matched for ever."""
    _res, rows = _load(db_session, [spelling])
    assert [r.region for r in rows] == ["Auckland"]


# ---- the whole file, in one go ---------------------------------------------
def test_a_mixed_file_keeps_the_auckland_rows_and_only_those(db_session):
    """The shape of the real complaint: a file that is mostly this region, with
    the spellings mixed, losing three quarters of itself."""
    regions = (["auckland"] * 5 + ["North Shore City"] * 3 + ["奥克兰"] * 2
               + [None] * 2 + ["region-14"] * 2 + ["Waikato"] * 2
               + ["Otago"] * 1)
    _res, rows = _load(db_session, regions)

    assert len(rows) == 14, "the Auckland rows did not all survive"
    assert {r.region for r in rows} == {"Auckland"}


# ---- the line people actually read ------------------------------------------
def test_the_rejection_summary_names_the_region_strings_it_refused(db_session):
    """"10,608 not in this region" on a file called auckland_v2.csv is a fact
    nobody can act on, and it is the line that gets pasted into chat. The strings
    have been in the run log for two builds; the summary is where they are read.
    """
    from app.models import ImportBatch as IB

    _load(db_session, ["Auckland"] * 3 + ["Waikato"] * 5 + ["Otago"] * 2)
    batch = (db_session.query(IB)
             .filter(IB.batch_type == "for_sale")
             .order_by(IB.id.desc()).first())

    assert "not in this region" in (batch.note or "")
    assert "Waikato" in batch.note and "Otago" in batch.note


def test_a_clean_load_does_not_grow_the_explanation(db_session):
    """No refused rows, nothing to explain."""
    from app.models import ImportBatch as IB

    _load(db_session, ["Auckland"] * 3)
    batch = (db_session.query(IB)
             .filter(IB.batch_type == "for_sale")
             .order_by(IB.id.desc()).first())

    assert "region column reads" not in (batch.note or "")


# ---- the real file ----------------------------------------------------------
def test_the_string_from_the_customers_export_is_read_as_auckland(db_session):
    """Not merely tolerated as unreadable — recognised.

    9.999991 already loaded this row, because an unreadable region string is not
    evidence a house is somewhere else. But "Auckland Great Area" is not
    unreadable; it is Auckland with three decorating words on it, and reading it
    as Auckland is the difference between a row that loads and a row that loads
    while the run log warns nobody knows where it is.
    """
    from app.ingest import region_is_unreadable, region_matches

    assert region_matches("Auckland Great Area", "Auckland")
    assert not region_is_unreadable("Auckland Great Area", "Auckland")


def test_stripping_those_words_does_not_swallow_a_real_region_name(db_session):
    """"Bay of Plenty" is a region name with a small word inside it, so "of" and
    "the" are deliberately not on the strip list. And a decorated name from
    somewhere else has to stay somewhere else."""
    from app.ingest import _region_key, region_matches

    assert _region_key("Bay of Plenty") == "bay of plenty"
    assert _region_key("West Coast") == "west coast"
    assert not region_matches("Greater Wellington", "Auckland")
    assert not region_matches("Waikato Region", "Auckland")
    assert not region_matches("Hamilton City", "Auckland")
    assert region_matches("Greater Wellington", "Wellington")
