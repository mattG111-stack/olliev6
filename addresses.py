"""One way to reduce an address so two sources can agree it is the same house.

There were two, and the weaker one was doing the job that matters most.

app.trademe.address_key compares only the STREET — everything after the first
comma is a different number of location parts depending on who wrote it — with
the suburb kept alongside so two "12 Queen Street"s stay apart. It normalises a
unit slash and expands the street type, and it is careful about where: only the
LAST word can be a street type, so "St Heliers Road" keeps its saint.

app.prior_price had its own, and it did none of that. It glued the suburb onto
the end of the whole address and expanded abbreviations anywhere in the string.
Two consequences, both silent:

    "36 Lloyd Avenue, Mount Albert" + suburb "Mount Albert"
        -> "36 lloyd avenue mount albert mount albert"
    "36 Lloyd Ave" + suburb "Mount Albert"
        -> "36 lloyd avenue mount albert"

    "3/107 Donovan Street"  ->  "3/107 donovan street"
    "3 / 107 Donovan St"    ->  "3 / 107 donovan street"

Neither pair matches. That function is what the carry-forward uses to find last
week's price for this week's listing, so a house whose address gained or lost a
suburb between two loads — or whose unit number gained a space — simply had no
prior price, reported as "nothing to carry" and indistinguishable from a house
that genuinely had none.

The algorithm here is trademe's, unchanged, because PortalListing.address_key is
STORED using it and a changed key would stop a portal listing matching itself
between sweeps. This module is where it lives now; both callers import it.
"""
from __future__ import annotations

import re

_UNIT = re.compile(r"^(\d+)\s*[/\\]\s*")
_PUNCT = re.compile(r"[^\w/\s]")

# Street types, written out. Every source abbreviates differently — our export
# says "Donovan Street", a portal says "Donovan St", and as bare text those are
# two different houses. Matching on the long form makes them one.
STREET_TYPES = {
    "st": "street", "str": "street", "rd": "road", "ave": "avenue",
    "av": "avenue", "dr": "drive", "drv": "drive", "pl": "place",
    "cres": "crescent", "cr": "crescent", "tce": "terrace", "ter": "terrace",
    "ln": "lane", "cl": "close", "ct": "court", "crt": "court",
    "gr": "grove", "grv": "grove", "pde": "parade", "prd": "parade",
    "hwy": "highway", "sq": "square", "blvd": "boulevard", "bvd": "boulevard",
    "hts": "heights", "esp": "esplanade", "qy": "quay", "mt": "mount",
}
# Only the LAST word is a street type. "St Heliers Road" starts with a saint,
# and "Mount Street" is not "mount street" the suburb — rewriting either would
# invent an address rather than normalise one.
_ABBREV = re.compile(r"\b(" + "|".join(sorted(STREET_TYPES, key=len, reverse=True))
                     + r")\.?$")


def expand_street_type(street: str) -> str:
    return _ABBREV.sub(lambda m: STREET_TYPES[m.group(1)], street)


def address_key(address, suburb=None) -> str | None:
    """A street address reduced to something two sources can agree on.

    Trade Me writes "1 Abernethy Way, Patumahoe, Pukekohe"; our export writes
    "3/107 Donovan Street, Blockhouse Bay, Auckland City, Auckland". Everything
    after the street is a different number of location parts in each, so only
    the street itself is compared, with the suburb alongside it to keep two
    "12 Queen Street"s in different suburbs apart.

    Unit numbers are KEPT. building_key() strips them because it is answering
    "which building is this", and that is the opposite question: 2/14 Queen St
    and 5/14 Queen St are different houses with different prices.
    """
    if not address:
        return None
    street = str(address).split(",")[0].strip().lower()
    street = _PUNCT.sub(" ", street)
    street = re.sub(r"\s+", " ", street).strip()
    if not street:
        return None
    # "3 / 107 donovan street" and "3/107 donovan street" are one address.
    street = _UNIT.sub(r"\1/", street.replace(" / ", "/"))
    # "donovan st" and "donovan street" are one address too.
    street = expand_street_type(street)
    sub = re.sub(r"\s+", " ", str(suburb or "").strip().lower())
    # And the suburb written into the street with no comma to split on:
    # "12 Elliot Street Remuera" one week, "12 Elliot St, Remuera" the next.
    # Taking only the part before the comma handles the second and not the
    # first, so the same house came out with two keys and the carry-forward
    # found no prior price for it.
    #
    # Only when something is left afterwards — an address that IS just the
    # suburb is not an address, and reducing it to nothing would match every
    # other one like it.
    if sub and street.endswith(" " + sub):
        trimmed = street[: -(len(sub) + 1)].strip()
        if trimmed:
            street = trimmed
    return f"{street}|{sub}"
