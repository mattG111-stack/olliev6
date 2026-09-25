"""Pure field readers for historical imported records. No network or credentials."""
import re

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def num(v) -> float | None:
    """A number out of whatever an actor put in the field.

    Actors are written by different people against different pages, so the same
    idea arrives as 1250000, "1,250,000", "$1.25m", "210 m²" or "".

    Take the FIRST number and read the unit that follows it. The previous
    version deleted every non-digit and parsed what was left, which is wrong in
    both directions:

        "210 m2"  -> "2102"   a 210 m² floor area read as 2,102 m²
        "182m²"   -> "182²"   ValueError, so a real area read as missing
                              (superscript two answers True to isdigit())

    A ten-fold floor area is not a wrong tile, it is a wrong valuation — floor
    area drives the $/m² comp rate — so this parses rather than strips.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    s = str(v).strip().lower().replace(",", "").replace("$", "")
    if not s:
        return None
    m = _NUMBER.search(s)
    if not m:
        return None
    try:
        n = float(m.group())
    except ValueError:
        return None

    # A multiplier counts only when it is the whole of what follows the number:
    # "1.25m" is $1.25 million, "182m²" is 182 square metres.
    tail = s[m.end():].strip()
    if tail == "m":
        n *= 1_000_000
    elif tail == "k":
        n *= 1_000
    elif tail.startswith("ha"):
        n *= 10_000                              # hectares, for rural land areas
    return n or None


def pick(item: dict, *names: str):
    """First present value among `names`, searching one level of nesting.

    Actors disagree about spelling — floorArea, floor_area, floorAreaM2 — and
    some nest the interesting parts under "property" or "attributes".
    """
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    for holder in ("property", "attributes", "details", "estimate", "valuation"):
        nested = item.get(holder)
        if isinstance(nested, dict):
            for name in names:
                if name in nested and nested[name] not in (None, ""):
                    return nested[name]
    return None


