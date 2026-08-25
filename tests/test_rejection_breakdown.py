"""Why rows were rejected, not just how many.

    "✓ 2,141 rows inserted · 11773 rejected · batch #36 why is it rejecting so
     many ?"

The answer was already in the database and there was no way to look at it.

Ingest counts every rejection against one of nine named reasons as it goes, and
writes the tally into batch.note — a column the upload history has never
displayed and the API has never returned. So a load reporting 11,773 rejections
gave no way at all to tell

    a feed that is mostly apartments (deliberately excluded)
    from a feed with no council valuations (nothing can price those)
    from a broken column mapping (which is a bug and needs fixing today)

Every one of those needs a completely different response, and on screen they
looked identical.

The note also read "rejected: apartment_excluded=4200, no_cv=3100", which is
variable names rather than English, and unranked — so the biggest cause was
wherever the dict happened to put it.
"""
from __future__ import annotations

import pytest


def test_the_history_api_returns_the_reason(db_session):
    from app.routers.admin_upload import HistoryRow

    assert "note" in HistoryRow.model_fields, (
        "the breakdown is computed, stored, and still not returned")


def test_every_rejection_reason_has_words_for_it():
    """A reason the ingest can emit but the translation table has never heard of
    prints a variable name to an operator."""
    import inspect
    import re

    from app import ingest

    src = inspect.getsource(ingest)
    emitted = set(re.findall(r'rejected_reasons\["([a-z_]+)"\]', src))
    # The translation table lives inside ingest_for_sale.
    translated = set(re.findall(r'^\s+"([a-z_]+)": "', src, re.M))
    missing = emitted - translated
    assert not missing, f"no plain-English wording for: {sorted(missing)}"


def test_the_reasons_are_ranked_biggest_first():
    """Unranked, the biggest cause is wherever the dict happened to put it."""
    import inspect

    from app import ingest

    src = inspect.getsource(ingest)
    assert "key=lambda kv: -kv[1]" in src


def test_the_wording_is_english_not_variable_names():
    import inspect

    from app import ingest

    src = inspect.getsource(ingest)
    assert "no council valuation, which every valuation method needs" in src
    assert "apartments (deliberately excluded" in src
    # And the old jargon form is gone.
    assert 'f"{k}={v}" for k, v in rejected_reasons.items()' not in src


def test_the_reasons_cover_what_a_hougarden_feed_actually_hits():
    """The three that account for most of a 146MB Auckland feed."""
    import inspect

    from app import ingest

    src = inspect.getsource(ingest)
    for reason in ("apartment_excluded", "no_cv", "asking_vs_cv_50pct"):
        assert f'rejected_reasons["{reason}"]' in src


# ---- "where is all the sold data gone?" -------------------------------------
#
# It had not gone anywhere. The tile counted the STAGED sold batch, so uploading
# a for-sale file on its own showed "SOLD ROWS 0" — which reads as data loss and
# means "there is no sold file in this upload".
#
# Sold data accumulates across published batches by design: a batch is a
# delivery, not the dataset. Proof it was still there the whole time: the 2,141
# listings in that batch were priced with comps, one of them off 36 sales, and
# comps come from the sold history.

def test_the_summary_reports_every_sold_row_not_just_this_upload(db_session):
    from app.models import BatchType, ImportBatch, PropertySold
    from app.release import staged_summary

    published = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                            filename="sold-history.xlsx", status="published",
                            is_active=True)
    db_session.add(published)
    db_session.flush()
    for i in range(25):
        db_session.add(PropertySold(import_batch_id=published.id,
                                    address=f"{i} Sold St", suburb="Riverhead",
                                    sale_price=1_000_000))
    # A for-sale upload on its own — no staged SOLD batch at all.
    db_session.add(ImportBatch(batch_type=BatchType.FOR_SALE.value,
                               region="Auckland", filename="listings.csv",
                               status="staged", rows_inserted=2141))
    db_session.commit()

    s = staged_summary(db_session, "Auckland")
    assert s.sold_rows == 0, "this upload genuinely carried no sold file"
    assert s.sold_total == 25, (
        "the sold history is still there and the page must be able to say so")


def test_a_sold_upload_counts_in_both(db_session):
    from app.models import BatchType, ImportBatch, PropertySold
    from app.release import staged_summary

    b = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                    filename="this-week-sold.xlsx", status="staged",
                    rows_inserted=7)
    db_session.add(b)
    db_session.flush()
    for i in range(7):
        db_session.add(PropertySold(import_batch_id=b.id, address=f"{i} New St",
                                    suburb="Riverhead", sale_price=900_000))
    db_session.commit()
    s = staged_summary(db_session, "Auckland")
    assert s.sold_rows == 7 and s.sold_total == 7


def test_the_api_returns_the_total(db_session):
    from app.routers.release import StagedOut

    assert "sold_total" in StagedOut.model_fields


# ---- a blocked lookup has to be visible ------------------------------------
def test_the_enrich_result_says_how_many_were_blocked():
    """It read "done: done · 2,141/2,141 looked up · 0 filled · 0 missed" while
    1,994 of those lookups were REFUSED. Filled-nothing and refused-everything
    look identical that way and need opposite responses."""
    import inspect

    from app import staged_stages

    src = inspect.getsource(staged_stages.run_enrich_job)
    # The completion call, not any mention in a comment.
    assert 'status="completed", stage=_stage' in src, (
        "the completion still writes a fixed stage rather than the summary")
    assert "blocked · " in src


def test_the_enrich_stage_fits_the_column():
    """IngestJob.stage is String(64) and a longer value used to be silently
    truncated by the database."""
    import inspect

    from app import staged_stages

    src = inspect.getsource(staged_stages.run_enrich_job)
    assert "_stage[:64]" in src
