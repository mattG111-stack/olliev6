"""The last sale price arrives as "$1.35M" and was being read as nothing.

Found by counting columns in a real staged export: 9,102 listings, and

    Last sold price   63   (0.7%)
    Last sold date  5,167  (56.8%)

5,104 listings knew WHEN a house last sold and not for how much. The 63 that had
both came from the council-record lookup, which returns a number; every one from
the weekly file had the date and not the price.

The cause is a naming exception in the feed. Nearly every figure is written
twice — a display string and a _numeric twin — and the load reads the twin.
valuation_last_sold_value has no twin. It arrives as "$1.35M" and it was being
read with the plain float() path, which returns None for it and raises nothing.
The date beside it is stored as text, so it survived, and the gap between the
two columns is the only thing that made this visible at all.

It matters beyond the blank column: the last sale price is one of two fields
from the weekly file that reach the pricing engine. It is how a scraped "price"
that is really the last sale gets caught, and it is read again when a home is
relisted near what it fetched last time.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")

import pytest  # noqa: E402

from app.ingest import _common_property_payload  # noqa: E402
from app.pricing.assumptions import parse_money  # noqa: E402


@pytest.mark.parametrize("written,expected", [
    # The four shapes in the real file.
    ("$1.35M", 1_350_000.0),
    ("$599K", 599_000.0),
    ("$4.50M", 4_500_000.0),
    ("$275K", 275_000.0),
    # And the ones that already worked, which must keep working.
    ("$1,850,000", 1_850_000.0),
    ("1850000", 1_850_000.0),
    (1_850_000.0, 1_850_000.0),
])
def test_a_money_string_is_read_as_money(written, expected):
    assert parse_money(written) == expected


@pytest.mark.parametrize("junk", [None, "", "  ", "nan", "NaT", "POA",
                                  float("nan"), float("inf"), "$", "K"])
def test_something_that_is_not_money_is_not_guessed_at(junk):
    assert parse_money(junk) is None


def test_the_load_reads_it_rather_than_dropping_it():
    """The bug itself: the payload the load writes to the database."""
    payload = _common_property_payload({
        "address": "13 Paunui Street", "suburb": "Saint Heliers",
        "valuation_last_sold_value": "$1.35M",
        "valuation_last_sold_date": "2018-05-31",
    })
    assert payload["valuation_last_sold_value"] == 1_350_000.0
    assert payload["valuation_last_sold_date"] == "2018-05-31"


def test_the_price_and_the_date_go_missing_together_or_not_at_all():
    """What made this findable, turned into a rule. A row that has one and not
    the other means a parser is failing silently, whichever way round it is."""
    for value, date in (("$1.35M", "2018-05-31"), (None, None)):
        payload = _common_property_payload({
            "address": "1 Example Road", "suburb": "Remuera",
            "valuation_last_sold_value": value,
            "valuation_last_sold_date": date,
        })
        got_price = payload["valuation_last_sold_value"] is not None
        got_date = payload["valuation_last_sold_date"] is not None
        assert got_price == got_date, (
            f"{value!r}/{date!r} produced price={got_price} date={got_date}"
        )


def test_the_pricing_run_and_the_load_use_the_same_parser():
    """They had one each. The pricing run's copy understood "$1.35M" and the
    load's did not, so the number was right while a listing was being priced and
    gone by the time it was stored — the two disagreed about the same cell."""
    from app.pricing import pipeline

    assert pipeline._parse_money is parse_money
