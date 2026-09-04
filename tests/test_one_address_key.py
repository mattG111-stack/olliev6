"""Two address keys, and the carry-forward had the weaker one.

    "it had a price before and now its price by negtion"
    36 Lloyd Ave

The carry-forward finds last week's price by reducing both addresses to a key
and matching them. It had its own reduction, and app.trademe had a better one,
and the difference was invisible: a house whose address changed shape between
two loads simply had no prior price — reported as "nothing to carry", which
reads exactly like a house that genuinely never had one.

What the weaker one did:

    "36 Lloyd Avenue, Mount Albert" + suburb "Mount Albert"
        -> "36 lloyd avenue mount albert mount albert"
    "36 Lloyd Ave"                  + suburb "Mount Albert"
        -> "36 lloyd avenue mount albert"

The suburb is in the address on one line and not the other, so it lands twice
on one side and once on the other, and the two never match. Same for a unit
number that gained a space, and for every street type missing from its shorter
list — court, square, boulevard, heights, esplanade, quay.

The surviving algorithm is trademe's, unchanged, because PortalListing.
address_key is STORED with it: a changed key would stop a portal listing
matching itself between sweeps and quietly duplicate it.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")

import pytest  # noqa: E402

from app.addresses import address_key  # noqa: E402


# ---- one definition ---------------------------------------------------------
def test_there_is_only_one_address_key():
    """The bug was two of them. This is what stops a third."""
    import glob
    import re

    defs = [f for f in glob.glob("app/**/*.py", recursive=True)
            if re.search(r"^def address_key", open(f).read(), re.M)]
    assert defs == ["app/addresses.py"], defs


def test_both_callers_use_the_same_object():
    from app.prior_price import address_key as carry
    from app.trademe import address_key as portals

    assert carry is portals is address_key


# ---- the shapes that were silently missing each other -----------------------
@pytest.mark.parametrize("a,b,suburb", [
    # The suburb written into the address on one load and not the other.
    ("36 Lloyd Avenue, Mount Albert", "36 Lloyd Ave", "Mount Albert"),
    ("31 Lloyd Avenue, Mount Albert", "31 Lloyd Av", "Mount Albert"),
    # A unit number that gained a space.
    ("3/107 Donovan Street", "3 / 107 Donovan St", "Blockhouse Bay"),
    # Street types the shorter list did not have.
    ("12 Anzac Court", "12 Anzac Ct", "Epsom"),
    ("5 Ellerslie Square", "5 Ellerslie Sq", "Ellerslie"),
    ("9 Tamaki Boulevard", "9 Tamaki Blvd", "Mission Bay"),
    ("2 Sunset Heights", "2 Sunset Hts", "Torbay"),
    ("40 Marine Esplanade", "40 Marine Esp", "Devonport"),
    ("7 Princes Quay", "7 Princes Qy", "Auckland Central"),
    # Trailing punctuation and case.
    ("14 Queen St.", "14 QUEEN STREET", "Newmarket"),
])
def test_the_same_house_written_two_ways_is_one_key(a, b, suburb):
    assert address_key(a, suburb) == address_key(b, suburb)


# ---- and the counterweights -------------------------------------------------
def test_two_units_in_one_building_stay_apart():
    """building_key() strips the unit because it asks which BUILDING this is.
    This asks which house, and they have different prices."""
    assert address_key("2/14 Queen Street", "Newmarket") != \
        address_key("5/14 Queen Street", "Newmarket")


def test_the_same_street_in_two_suburbs_stays_apart():
    assert address_key("12 Queen Street", "Newmarket") != \
        address_key("12 Queen Street", "Papakura")


def test_a_saint_is_not_a_street():
    """Only the LAST word can be a street type. "St Heliers Road" starts with a
    saint, and expanding it there would invent an address."""
    assert address_key("5 St Heliers Road", "Glendowie").startswith("5 st heliers road")


def test_a_mount_in_the_middle_is_left_alone():
    assert "mount eden road" in address_key("18 Mount Eden Road", "Mount Eden")


@pytest.mark.parametrize("junk", [None, "", "   ", ","])
def test_nothing_in_gives_nothing_back(junk):
    assert address_key(junk, "Epsom") is None


# ---- what must not change ---------------------------------------------------
def test_the_stored_portal_keys_are_untouched():
    """PortalListing.address_key is written to the database with this function.
    A changed key would stop a portal listing matching itself between sweeps and
    quietly duplicate it, so the algorithm moved modules and nothing else."""
    assert address_key("3/107 Donovan Street, Blockhouse Bay, Auckland City",
                       "Blockhouse Bay") == "3/107 donovan street|blockhouse bay"
    assert address_key("1 Abernethy Way, Patumahoe, Pukekohe",
                       "Patumahoe") == "1 abernethy way|patumahoe"
