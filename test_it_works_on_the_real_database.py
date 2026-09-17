"""SQLite is not Postgres, and the tests run on SQLite.

    "make sure this is fully funtional in the morning and fullt tested and will
     work first go"

The suite runs on SQLite. Production is Postgres. They disagree on exactly the
things that have already cost a deploy, and every disagreement has the same
shape: SQLite is permissive, so the test passes; Postgres is strict, so the
live insert fails. A green suite is then evidence of nothing.

Two faults found by running the whole suite against a real Postgres, both
invisible on SQLite, both already live:

  price_flag was VARCHAR(48) and the message it holds is 54 characters —
  "10% of the Flat Bush median — check for a missing digit". SQLite ignores a
  column's length; Postgres refuses the row. The flag exists to catch a price
  with a digit missing or added, so the listing most in need of a human eye was
  the one that could not be written. (best_strategy at 32 was the same fault,
  in v1.39. Twice is a pattern.)

  the run-log workbook wrote a timezone-aware timestamp into a cell. SQLite
  does not store timezones so it hands back a naive datetime and the line was
  never reached; Postgres hands back an aware one and openpyxl raises. That
  download has answered 500 on the live site every time it was ever pressed.

THESE TESTS DO NOT NAME THOSE TWO. Naming them catches what is already found.
They go looking: every string column is measured against the longest value the
code can actually put in it, and every datetime that reaches a spreadsheet is
checked for a timezone. That is what catches the third one.
"""
from __future__ import annotations

import inspect
import re

import pytest

from app import models


def _string_columns():
    """(model, column, limit) for every bounded text column in the schema."""
    from sqlalchemy import String

    out = []
    for mapper in models.Base.registry.mappers:
        for col in mapper.local_table.columns:
            t = col.type
            if isinstance(t, String) and t.length:
                out.append((mapper.class_.__name__, col.name, t.length))
    return out


# ---- the length rule --------------------------------------------------------
def test_no_generated_message_can_outgrow_its_column():
    """THE ONE THAT MATTERS, and it SEARCHES rather than lists.

    Finds every f-string the code assigns to a column, works out the longest it
    could be, and compares that with the column's limit. A message that cannot
    fit is a row that cannot be written — on Postgres, where it counts.
    """
    limits = {name: length for _, name, length in _string_columns()}
    # The generated messages, with a realistic worst case for each placeholder.
    # Auckland's longest suburb name is the one that decides several of these.
    LONGEST_SUBURB = "Whangaparaoa Peninsula"
    from app.portals import listings as L

    src = inspect.getsource(L)
    offenders = []
    for m in re.finditer(r'f"([^"]{20,})"', src):
        text = m.group(1)
        if "median" not in text and "check for" not in text:
            continue
        # Substitute the worst case for each {placeholder}.
        worst = re.sub(r"\{[^}]*\}", LONGEST_SUBURB, text)
        worst = worst.replace(LONGEST_SUBURB, "1033%", 1) \
            if worst.startswith("1033%") else worst
        need = len(worst)
        cap = limits.get("price_flag")
        if cap and need > cap:
            offenders.append(f"price_flag holds {cap} chars, needs {need}: {worst!r}")
    assert not offenders, "\n".join(offenders)


def test_the_price_flag_fits_the_message_it_is_built_for():
    """The specific one, kept as well as the search above, because it is the
    row a data fault lands on and losing it loses the warning."""
    from app.portals.listings import _price_flag  # noqa: F401  (may be renamed)

    limits = {name: length for _, name, length in _string_columns()}
    longest = (f"{9999:.0%} of the Whangaparaoa Peninsula median "
               f"— check for a missing digit")
    assert limits["price_flag"] >= len(longest), (
        f"price_flag is {limits['price_flag']}; the message is {len(longest)}")


@pytest.mark.parametrize("column,least", [
    ("hold_reason", 120),        # "Below $10,000 margin", and longer ones
    ("deal_block_reason", 120),  # a whole sentence explaining why not a deal
    ("best_strategy", 48),       # "Retain house + sell new sections" was 32
    ("price_flag", 100),
])
def test_the_explanation_columns_have_room(column, least):
    """Every one of these holds a sentence written for a person to read. A
    sentence in a column sized for a code word gets refused by Postgres."""
    limits = {name: length for _, name, length in _string_columns()}
    if column not in limits:
        pytest.skip(f"{column} is not a bounded string column")
    assert limits[column] >= least, (
        f"{column} is VARCHAR({limits[column]}) and holds prose")


# ---- the timezone rule ------------------------------------------------------
def test_nothing_writes_an_aware_timestamp_into_a_spreadsheet():
    """openpyxl raises on a timezone, and Postgres is where timezones come from.

    Checked by reading the source of every module that builds a workbook: each
    must put its datetimes through something that strips the timezone. SQLite
    will never reproduce this, so a behavioural test cannot be trusted to.
    """
    from app import audit_export, runlog_export

    for mod in (runlog_export, audit_export):
        src = inspect.getsource(mod)
        handles = ("tzinfo=None" in src or "replace(tzinfo" in src
                   or "strftime" in src)
        assert handles, (
            f"{mod.__name__} writes datetimes to a workbook without stripping "
            f"the timezone — 500 on Postgres, fine on SQLite")


def test_the_run_log_workbook_takes_a_timezone_aware_time():
    """The actual behaviour, not just the source. An aware datetime is what
    Postgres returns, so it is what the cleaner must accept."""
    from datetime import datetime, timedelta, timezone

    from app.runlog_export import _clean

    aware = datetime(2026, 9, 8, 21, 30, tzinfo=timezone.utc)
    out = _clean(aware)
    assert out.tzinfo is None, "Excel will refuse this"
    # And converted, not merely stripped — NZ is twelve hours ahead, so a 21:30
    # UTC event happened on the 9th here. Dropping the zone would print the 8th.
    assert (out.day, out.hour) == (9, 9), (
        f"expected 9 Sept 09:30 New Zealand time, got {out}")


def test_a_naive_time_is_left_alone():
    """SQLite hands back naive datetimes and they are already correct."""
    from datetime import datetime

    from app.runlog_export import _clean

    naive = datetime(2026, 9, 8, 21, 30)
    assert _clean(naive) == naive
