"""When CoreLogic can't be reached at all.

    "it didnt fill 2141 listings at all"

The screen said 2,141 looked up, 0 filled. Read plainly that means CoreLogic
answered two thousand times and had nothing useful — a data limit you accept and
move on from. It is also what a total outage looks like, and that is the
opposite situation: nothing was asked, everything is still there to fetch.

Two separate faults made those indistinguishable.

  THE COUNT. A request that failed before it arrived — DNS, proxy, TLS, timeout
  — came back as "no record", identical to an address CoreLogic genuinely does
  not hold. The summary then reported all of them as "not in CoreLogic", which
  points the operator at the addresses. The addresses were fine.

  THE STAMP, which is the one that made it permanent. pv_checked_at was written
  on anything that wasn't a 401/403/429, transport failures included. The work
  list skips stamped rows. So one run that could not reach CoreLogic marked all
  2,141 rows checked, and every re-run afterwards found an empty work list and
  finished instantly. The batch could never be enriched again — not by waiting,
  not by re-running, not by fixing the network.

A block was already handled correctly: it leaves the row unstamped so the next
wave retries it. An unreachable host is the same kind of non-answer and now gets
the same treatment.
"""
from __future__ import annotations

import pytest

from app.models import (BatchType, ImportBatch, IngestJob, PropertyForSale,
                        PropertySold)
from app.propertyvalue import PV_BLOCKED, PV_ERROR, PV_NOT_FOUND, PV_OK


def _batch(db, rows=3):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="auckland.csv", is_active=False, status="staged")
    db.add(b)
    db.flush()
    for i in range(rows):
        db.add(PropertyForSale(
            import_batch_id=b.id, address=f"{i} Unreachable Road", suburb="Riverhead",
            asking_price=900_000.0, cv_numeric=1_000_000.0,
            floor_area_m2=None, land_area_m2=None))   # blank -> needs a lookup
    db.commit()
    return b


def _job(db, batch):
    j = IngestJob(batch_type=BatchType.FOR_SALE.value, filename="auckland.csv",
                  file_size_bytes=1, status="running", batch_id=batch.id)
    db.add(j)
    db.commit()
    return j


def _run(db, batch, job, monkeypatch, answers):
    """Run the enrich stage with pv_lookup_status replaced by a canned sequence.

    The worker opens its own session (it is a background thread in production),
    so the fixture commits first and expires afterwards rather than handing its
    session in.
    """
    import app.staged_stages as st

    seq = list(answers)
    monkeypatch.setattr(st, "pv_lookup_status",
                        lambda q, *a, **k: seq.pop(0) if seq else (None, PV_ERROR))
    monkeypatch.setattr(st.time, "sleep", lambda *_: None)
    db.commit()
    st.run_enrich_job(job.id, batch.id, "Auckland", delay=0)
    db.expire_all()
    return db.get(IngestJob, job.id)


# ---- the fault that made it permanent --------------------------------------
def test_an_unreachable_host_does_not_mark_the_rows_checked(db_session, monkeypatch):
    """The whole batch stayed enrichable, or it didn't. Everything else is
    reporting; this is the part that decided whether a re-run could ever work."""
    b = _batch(db_session, rows=3)
    _run(db_session, b, _job(db_session, b), monkeypatch, [(None, PV_ERROR)] * 3)

    rows = db_session.query(PropertyForSale).filter_by(import_batch_id=b.id).all()
    assert all(p.pv_checked_at is None for p in rows), \
        "a transport failure was recorded as a CoreLogic answer"


def test_a_rerun_after_an_outage_retries_every_row(db_session, monkeypatch):
    """The proof of the above, end to end: outage, then the network comes back,
    then a re-run actually fills the rows. Before the fix the second run had an
    empty work list and completed in milliseconds having done nothing."""
    b = _batch(db_session, rows=3)
    _run(db_session, b, _job(db_session, b), monkeypatch, [(None, PV_ERROR)] * 3)

    good = ({"floor_area_m2": 150.0, "land_area_m2": 600.0}, PV_OK)
    job2 = _run(db_session, b, _job(db_session, b), monkeypatch, [good] * 3)

    assert job2.rows_inserted == 3, "the re-run skipped the rows it never reached"
    rows = db_session.query(PropertyForSale).filter_by(import_batch_id=b.id).all()
    assert all(p.floor_area_m2 == 150.0 for p in rows)


def test_an_address_genuinely_not_held_is_still_marked_checked(db_session, monkeypatch):
    """The counterweight. "CoreLogic doesn't have this one" IS an answer, and
    re-buying it every week is exactly what the stamp exists to prevent."""
    b = _batch(db_session, rows=2)
    _run(db_session, b, _job(db_session, b), monkeypatch, [(None, PV_NOT_FOUND)] * 2)

    rows = db_session.query(PropertyForSale).filter_by(import_batch_id=b.id).all()
    assert all(p.pv_checked_at is not None for p in rows)


def test_a_block_is_still_not_an_answer(db_session, monkeypatch):
    """Unchanged behaviour, asserted so the fix above can't quietly undo it."""
    b = _batch(db_session, rows=1)
    _run(db_session, b, _job(db_session, b), monkeypatch, [(None, PV_BLOCKED)] * 20)

    p = db_session.query(PropertyForSale).filter_by(import_batch_id=b.id).first()
    assert p.pv_checked_at is None


# ---- the reporting ---------------------------------------------------------
def test_the_summary_says_unreachable_not_not_found(db_session, monkeypatch):
    """"2,141 addresses not in CoreLogic" sends you to look at the addresses.
    That is the wrong place, and it costs a day."""
    b = _batch(db_session, rows=2)
    job = _run(db_session, b, _job(db_session, b), monkeypatch, [(None, PV_ERROR)] * 2)

    assert "unreachable" in (job.stage or "")
    assert "not found 2" not in (job.stage or "")


def test_the_message_says_a_rerun_will_retry_them(db_session, monkeypatch):
    """An operator's next question is "so do I run it again". Answer it in the
    message rather than leaving it to be worked out."""
    b = _batch(db_session, rows=2)
    job = _run(db_session, b, _job(db_session, b), monkeypatch, [(None, PV_ERROR)] * 2)

    msg = (job.error_message or "")
    assert "never reached" in msg
    assert "re-run" in msg.lower()


def test_a_clean_run_reports_no_unreachable_lookups(db_session, monkeypatch):
    """A counter that always shows something is noise. Absent when it is zero."""
    b = _batch(db_session, rows=2)
    good = ({"floor_area_m2": 120.0, "land_area_m2": 400.0}, PV_OK)
    job = _run(db_session, b, _job(db_session, b), monkeypatch, [good] * 2)

    assert "unreachable" not in (job.stage or "")
    assert not job.error_message


def test_not_found_and_unreachable_are_counted_apart(db_session, monkeypatch):
    """Mixed run — one real miss, one outage. The two numbers must not be one."""
    b = _batch(db_session, rows=2)
    job = _run(db_session, b, _job(db_session, b), monkeypatch,
               [(None, PV_NOT_FOUND), (None, PV_ERROR)])

    assert "1 not found" in (job.stage or "")
    assert "1 unreachable" in (job.stage or "")


def test_the_stage_line_still_fits_the_column(db_session, monkeypatch):
    """IngestJob.stage is String(64). Postgres truncates a longer value without
    saying so, and a status that silently loses its tail is how the blocked
    count went missing the first time."""
    b = _batch(db_session, rows=2)
    job = _run(db_session, b, _job(db_session, b), monkeypatch,
               [(None, PV_NOT_FOUND), (None, PV_ERROR)])

    assert len(job.stage or "") <= 64


def test_a_total_outage_fails_the_job_and_names_the_cause(db_session, monkeypatch):
    """Forty nothings in a row already stopped the run. It used to guess at why;
    when every one of them failed before reaching CoreLogic, that is not a guess."""
    b = _batch(db_session, rows=45)
    job = _run(db_session, b, _job(db_session, b), monkeypatch, [(None, PV_ERROR)] * 45)

    assert job.status == "failed"
    assert "before reaching CoreLogic" in (job.error_message or "")
    rows = db_session.query(PropertyForSale).filter_by(import_batch_id=b.id).all()
    assert all(p.pv_checked_at is None for p in rows), \
        "a failed run must leave every row retryable"


# ---- why only 147 of 2,141 ---------------------------------------------------
#
#   "but it didnt even try do all of them ?"
#
# It did not, and it was not a failure — it asked about every row it had a
# reason to ask about and finished. The reason was "is a floor area, land area
# or CV missing", and nearly every row already has all three.
#
# But the same lookup also returns the LAST SALE, and nothing was asking for it.
# `Last sold` was empty on all 2,141 rows of the export and would have stayed
# empty for ever: a row with a floor area was never eligible to be asked, so its
# sale history was never fetched, so it was never eligible. A previous sale
# price is shown on the listing, it is how a scraped "price" that is really the
# last sale gets caught, and it is the only independent read on a property whose
# council record is stale.
def test_a_complete_row_with_no_sale_history_is_still_looked_up(db_session, monkeypatch):
    b = _batch(db_session, rows=0)
    # A real deal candidate — a fair value above the asking, so the pre-publish
    # holds leave it in the feed. A row held for "no deal here" is deliberately
    # skipped by enrich and would test the wrong thing.
    db_session.add(PropertyForSale(
        import_batch_id=b.id, address="12 Elliot Street", suburb="Remuera",
        asking_price=1_000_000.0, cv_numeric=1_100_000.0, fair_value=1_200_000.0,
        margin=0.20, confidence="high", comps_used=9,
        floor_area_m2=150.0, land_area_m2=600.0))     # nothing missing but the sale
    db_session.commit()

    job = _run(db_session, b, _job(db_session, b), monkeypatch,
               [({"last_sale_price": 900_000.0}, PV_OK)])

    assert job.rows_total == 1, "a row with no sale history was never asked about"


def test_a_row_that_already_has_its_sale_history_is_left_alone(db_session, monkeypatch):
    """The counterweight. Asking again buys nothing and, across a weekly load,
    turns a few hundred lookups into a few thousand for no new information."""
    b = _batch(db_session, rows=0)
    db_session.add(PropertyForSale(
        import_batch_id=b.id, address="12 Elliot Street", suburb="Remuera",
        asking_price=1_000_000.0, cv_numeric=1_100_000.0, fair_value=1_200_000.0,
        margin=0.20, confidence="high", comps_used=9,
        floor_area_m2=150.0, land_area_m2=600.0,
        valuation_last_sold_value=900_000.0))
    db_session.commit()

    job = _run(db_session, b, _job(db_session, b), monkeypatch,
               [({"cv": 1}, PV_OK)])

    assert job.rows_total == 0


def test_the_run_says_how_much_of_the_batch_it_is_asking_about(db_session, monkeypatch):
    """"147/147 looked up" on a 2,141-row load reads as a run that gave up a
    fourteenth of the way in. The total it is measured against has to be stated
    next to the size of the batch, or the number is unreadable."""
    from app.runlog import events

    b = _batch(db_session, rows=3)
    _run(db_session, b, _job(db_session, b), monkeypatch, [({"cv": 1}, PV_OK)] * 3)

    planned = [e for e in events(db_session, b.id) if e.event == "lookups_planned"]
    assert planned, "nothing recorded how many rows the run intended to look up"
    assert "of 3" in (planned[0].detail or ""), planned[0].detail
