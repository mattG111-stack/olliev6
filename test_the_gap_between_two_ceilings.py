"""Two ceilings for one rule, and a listing living in the gap.

    "how did we get too this number ?"
    42A Woodlands Crescent — $999k asking, $1.79M valuation, 79.6% margin,
    headlined as the biggest gap on the market. A 3-bed 1-bath.

Two numbers said how far a valuation may sit above an asking price and still be
a find, and they disagreed. The hedonic refused anything over 1.6x, with the
note that genuine below-value deals top out around +50%. The deal guard in the
pricing run said 1.8x. Nothing was between them on purpose — nobody had noticed
they were the same rule written twice.

42A sat at 1.794x. Four thousandths under the guard that would have stopped it,
and well over the one that already said it was implausible.

The valuation is CV-anchored, so the hedonic's own check never touches it — the
1.6x there is applied to the hedonic's estimate, not to what gets published. The
only thing standing between a published valuation and an asking price was the
1.8x, and it was the looser of the two.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from app.pricing.assumptions import MAX_VALUE_VS_ASKING  # noqa: E402
from app.pricing.comps import SoldDataset  # noqa: E402
from app.pricing.pipeline import run  # noqa: E402

ASKING = 999_000


def _priced(comp_price: int, cv: int):
    """One listing at $999,000, against comps that value it at roughly
    comp_price. Returns the row the pipeline produces."""
    sold = SoldDataset(pd.DataFrame([{
        "suburb": "Browns Bay", "district": "North Shore",
        "property_type": "House", "key_bedrooms": 3, "key_bathrooms": 1,
        "key_floor_area": f"{120 + i} sqm", "key_land_area": f"{600 + i} sqm",
        "cv_numeric": cv, "price_numeric": comp_price + i * 5_000,
        "sale_price": comp_price + i * 5_000,
        "land_value_numeric": int(cv * 0.75),
        "improvement_value_numeric": int(cv * 0.25),
        "type_of_title": "Freehold", "sold_date": "2026-05-01",
    } for i in range(14)]))
    row = dict(address="42A Woodlands Crescent", suburb="Browns Bay",
               district="North Shore", property_type="House", cv_numeric=cv,
               key_floor_area=125, key_land_area=610, key_bedrooms=3,
               key_bathrooms=1, type_of_title="Freehold",
               land_value_numeric=int(cv * 0.75),
               improvement_value_numeric=int(cv * 0.25),
               price_numeric=ASKING, price_display=f"${ASKING:,}")
    return run(pd.DataFrame([row]), sold).iloc[0]


# ---- the listing that was on the front page ---------------------------------
def test_the_listing_that_was_headlined_is_refused():
    """Reproduced from the card: valued 1.79x the asking, 79.6% margin."""
    out = _priced(1_760_000, 1_650_000)

    assert out.get("fair_value") / ASKING > 1.7, "the reproduction has drifted"
    assert out.get("margin") is None, (
        f"still published as a deal at {out.get('margin'):.1%}")
    # bool(): the pipeline returns numpy booleans, and np.False_ is not False.
    assert bool(out.get("is_underpriced")) is False
    assert "not a find" in (out.get("deal_block_reason") or "")


# ---- and the deals that have to survive it ----------------------------------
@pytest.mark.parametrize("comps,cv,expect_pct", [
    (1_180_000, 1_100_000, 0.21),
    (1_280_000, 1_200_000, 0.31),
    (1_450_000, 1_350_000, 0.48),
])
def test_a_real_deal_still_gets_through(comps, cv, expect_pct):
    """The counterweight, and the reason the line is 1.6 and not lower. A
    ceiling that catches the broken row by also catching the good ones has not
    fixed anything — it has just emptied the page."""
    out = _priced(comps, cv)

    assert out.get("margin") is not None, "a genuine deal was refused"
    assert bool(out.get("is_underpriced")) is True
    assert out.get("margin") == pytest.approx(expect_pct, abs=0.02)


# ---- one rule, one place ----------------------------------------------------
def test_the_ceiling_is_defined_once():
    """The whole bug was two numbers for one idea. This is what stops the next
    one drifting apart from the other."""
    import glob
    import re

    assert 1.0 < MAX_VALUE_VS_ASKING < 3.0
    stray = []
    for f in glob.glob("app/**/*.py", recursive=True):
        if f.endswith("assumptions.py"):
            continue
        for line in open(f).read().splitlines():
            code = line.split("#", 1)[0]
            if re.search(r"(1\.8|1\.6)\s*\*\s*asking", code):
                stray.append(f"{f}: {line.strip()}")
    assert stray == [], (
        "the value-vs-asking ceiling is written out again in:\n  "
        + "\n  ".join(stray) + "\nUse assumptions.MAX_VALUE_VS_ASKING.")


def test_both_readers_import_it():
    """A constant nobody reads fixes nothing — ASKING_PRICE_MIN sat unused in
    assumptions while two modules wrote the literal."""
    for mod in ("app/pricing/pipeline.py", "app/pricing/glm.py"):
        assert "MAX_VALUE_VS_ASKING" in open(mod).read(), mod
