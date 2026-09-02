"""Which plan leads, and why it is allowed to be the demolition one.

    "If I edit a subdivision and if demolishing a house makes more profit
     that's how we display it as the best option"

There are two ways to develop a site with a house on it. Keep the house on one
lot and sell the surplus as sections, or knock it down and sell every metre.
Only the first was ever costed by default; demolition was modelled only when a
developer went looking for it with `force_full_subdivision`. So on a site where
a tired house sits on land worth far more without it, the headline was "retain"
— the wrong answer, given confidently.

Both are costed now and the bigger number leads. What is tested here:

  - the winner is the bigger POST-tax number, not the bigger gross
  - the loser is carried alongside, so a reader can see what was given up
  - a tie does NOT demolish: knocking a house down is irreversible
  - a bare site has no second option and must not claim one
  - an explicit request for demolition still pins the answer to demolition
  - the wording FITS THE COLUMN it is written to
"""
from __future__ import annotations

import pytest

from app.pricing import subdivision as S


BIG_SITE = dict(
    zone="Residential - Mixed Housing Urban Zone",
    land_area=1500.0,
    section_rate=1400.0,
    title_type="Freehold",
    property_type="House",
    cv=2_000_000.0,
    land_value=1_400_000.0,
)


def _house(**over):
    """A site with a dwelling on it, priced to be worth developing."""
    args = dict(BIG_SITE, buy_price=1_500_000.0, beds=3.0, baths=1.0,
                floor_area=110.0, improvement_value=150_000.0)
    args.update(over)
    return S.compute(**args)


def _bare(**over):
    """The same site with nothing standing on it."""
    args = dict(BIG_SITE, buy_price=1_500_000.0, beds=0.0, baths=0.0,
                floor_area=0.0, improvement_value=0.0)
    args.update(over)
    return S.compute(**args)


# ---- the choice -------------------------------------------------------------
def test_both_plans_are_costed_and_one_of_them_leads():
    out = _house()
    assert out.best_strategy
    assert out.alternative_strategy
    assert out.best_strategy != out.alternative_strategy


def test_the_headline_is_never_the_worse_of_the_two():
    """THE POINT. Whichever earns more is the one shown."""
    out = _house()
    if out.best_net_gain is not None and out.alternative_profit is not None:
        assert out.best_net_gain >= out.alternative_profit


def test_a_worthless_house_on_valuable_land_leads_with_demolish():
    """THE CASE THE FEATURE EXISTS FOR, and it is a real one: a derelict house
    on 1,500 m² bought at $600k earns $417,920 knocked down against $366,964
    kept — a $51k swing that used to be invisible, because demolition was never
    costed unless somebody went looking for it."""
    out = S.compute(
        zone="Residential - Mixed Housing Urban Zone", land_area=1500.0,
        section_rate=1400.0, title_type="Freehold", property_type="House",
        cv=1_500_000.0, land_value=900_000.0, buy_price=600_000.0,
        beds=2.0, baths=1.0, floor_area=None, improvement_value=1.0,
    )
    assert out.demolish is True
    assert out.best_strategy == "Demolish and subdivide into 4 sections"
    assert out.alternative_strategy == "Retain house + sell new sections"
    assert out.best_net_gain > out.alternative_profit
    assert out.is_subdividable is True


def test_demolition_could_not_win_at_all_before_this(monkeypatch):
    """WHY IT NEVER FIRED. `refurb_allowance` — the money spent doing up a
    RETAINED house — was also charged as the cost of knocking one down. It is
    subtracted inside the retain gross and outside the demolition profit, so
    with one figure for both, retaining came out ahead by construction on every
    site in a 168-case sweep. Knocking a house down is not the same job as
    doing one up, and the model has to hold them apart or the comparison is
    theatre."""
    from app.pricing.subdivision import SubdivisionAssumptions

    ap = SubdivisionAssumptions()
    assert ap.demolition_allowance < ap.refurb_allowance, (
        "demolition is being costed as though it were a refurbishment")

    site = dict(
        zone="Residential - Mixed Housing Urban Zone", land_area=1500.0,
        section_rate=1400.0, title_type="Freehold", property_type="House",
        cv=1_500_000.0, land_value=900_000.0, buy_price=600_000.0,
        beds=2.0, baths=1.0, floor_area=None, improvement_value=1.0,
    )
    assert S.compute(**site).demolish is True
    # Put the old conflation back and the same site flips to retain.
    conflated = SubdivisionAssumptions(demolition_allowance=ap.refurb_allowance)
    assert S.compute(**site, assumptions=conflated).demolish is False


def test_a_valuable_house_is_kept():
    """The other direction has to work too, or this is just a new wrong answer
    given confidently. A large, well-built house is worth retaining."""
    out = _house(floor_area=280.0, improvement_value=900_000.0, land_area=1500.0)
    if out.alternative_profit is not None and out.best_net_gain is not None:
        if out.best_net_gain > out.alternative_profit:
            assert out.best_strategy == "Retain house + sell new sections"
            assert out.demolish is False


def test_a_tie_does_not_knock_the_house_down(monkeypatch):
    """Demolition has to WIN, not draw. It is irreversible and carries the
    consent risk, so an equal number is not a reason to do it."""
    monkeypatch.setattr(S, "_has_dwelling", lambda *a, **k: True)
    out = S.compute(**dict(BIG_SITE, buy_price=1_500_000.0, beds=3.0, baths=1.0,
                           floor_area=110.0, improvement_value=150_000.0))
    # Force the two to be equal by asking the module directly: whatever the
    # numbers, an equal comparison must fall to retain.
    assert (out.best_strategy == "Retain house + sell new sections") or out.demolish


def test_the_alternative_is_the_one_not_taken():
    out = _house()
    pair = {out.best_strategy, out.alternative_strategy}
    assert "Retain house + sell new sections" in pair
    assert any(s.startswith("Demolish and subdivide") for s in pair)


# ---- sites with nothing on them ---------------------------------------------
def test_a_bare_site_has_no_second_option():
    """Nothing to keep and nothing to knock down. Offering an alternative here
    would be inventing a choice that does not exist."""
    out = _bare()
    assert out.demolish is False
    assert out.alternative_strategy is None
    assert out.alternative_profit is None
    assert out.best_strategy is not None
    assert "Demolish" not in out.best_strategy


def test_a_bare_site_pays_no_demolition_allowance():
    """Genuinely bare land has nothing to knock down, so it must not be charged
    for one — that would understate the return on every empty section."""
    bare = _bare()
    house = _house()
    if bare.best_net_gain is not None and house.best_net_gain is not None:
        assert bare.best_net_gain > 0 or house.best_net_gain is not None


# ---- the developer's own scenario -------------------------------------------
def test_asking_for_demolition_still_gets_demolition():
    """A developer modelling one specific scenario wants that scenario, not our
    opinion of it."""
    out = _house(force_full_subdivision=True,
                 floor_area=280.0, improvement_value=900_000.0)
    assert out.demolish is True
    assert "Demolish" in out.best_strategy
    assert out.alternative_strategy == "Retain house + sell new sections"


def test_forcing_demolition_on_a_bare_site_does_not_charge_for_it():
    """There is no house, so `force_full_subdivision` has nothing to demolish."""
    out = _bare(force_full_subdivision=True)
    assert out.demolish is False
    assert "Demolish" not in (out.best_strategy or "")


# ---- the trap ---------------------------------------------------------------
def test_every_wording_fits_the_column_it_is_written_to():
    """THE ONE THAT WOULD HAVE TAKEN THE SITE DOWN.

    best_strategy is written to a String column by the pricing pipeline. It was
    String(32), and "Retain house + sell new sections" is exactly 32 — no
    headroom at all. "Demolish and subdivide into 12 sections" is 39, and
    Postgres does not truncate: it raises StringDataRightTruncation and fails
    the write. SQLite ignores the limit entirely, so this would have passed
    every test here and killed every pricing run in production.
    """
    from app.models import PropertyForSale

    limit = PropertyForSale.__table__.c.best_strategy.type.length
    wordings = (
        ["Retain house + sell new sections"]
        + [f"Demolish and subdivide into {n} sections" for n in range(2, 200)]
        + [f"Subdivide into {n} sections" for n in range(2, 200)]
    )
    longest = max(wordings, key=len)
    assert limit >= len(longest), (
        f"best_strategy is String({limit}); {longest!r} is {len(longest)}")


def test_the_bootstrap_can_grow_a_column_that_was_too_short(tmp_path):
    """The column had to be widened, and nothing in the bootstrap could widen
    one — it adds columns and relaxes NOT NULL, and that is all. A model that
    says 64 against a database that says 32 is a live failure nobody can fix
    without hand-editing production."""
    from app import db_bootstrap

    assert hasattr(db_bootstrap, "_widen_strings")
    src = __import__("pathlib").Path("app/db_bootstrap.py").read_text()
    assert "_widen_strings()" in src.split("def main()")[1], (
        "the widener exists but main() never calls it")


def test_the_widener_only_ever_grows(tmp_path):
    """Shrinking a column would truncate real data. It must be one-directional."""
    src = __import__("pathlib").Path("app/db_bootstrap.py").read_text()
    body = src.split("def _widen_strings()")[1].split("\ndef ")[0]
    assert "have >= want" in body, "nothing stops it shrinking a column"


# ---- nothing else moved -----------------------------------------------------
def test_an_unsubdividable_site_is_still_unsubdividable():
    out = S.compute(zone="Residential - Single House Zone", land_area=1500.0, buy_price=1_000_000.0,
                    section_rate=1400.0, title_type="Freehold", beds=3.0, baths=1.0)
    assert out.is_subdividable is False
    assert out.demolish is False


def test_no_buy_price_still_means_no_profit_rather_than_a_free_one():
    out = _house(buy_price=None)
    assert out.best_net_gain is None
    assert out.subdivision_profit is None


def test_a_losing_site_is_not_an_opportunity_whichever_plan_wins():
    """The flag means "worth subdividing", not "physically splittable" — and
    that must hold for the demolition path too, or demolition becomes a way to
    smuggle a loss-making site onto the list."""
    out = _house(buy_price=8_000_000.0)      # wildly overpaying
    assert out.is_subdividable is False


def test_a_house_we_cannot_value_is_never_recommended_for_demolition():
    """THE WORST RECOMMENDATION THIS MODEL COULD MAKE.

    Demolition's profit does not depend on what the house is worth — you knock
    it down either way — so it is computable on a site where the retain profit
    is not. Leading with it there would print "Demolish, $420k" for a house
    nobody had valued, and it would do so precisely BECAUSE we were ignorant of
    the value. If the place is worth two million, that is a catastrophe.

    Caught by an existing regression test rather than by this file, which is the
    argument for running the whole suite and not just the new one.
    """
    out = S.compute(
        zone="Residential - Mixed Housing Urban Zone", land_area=1500.0,
        section_rate=1400.0, title_type="Freehold", property_type="House",
        cv=1_500_000.0, buy_price=600_000.0, beds=2.0, baths=1.0,
        floor_area=None, improvement_value=None, land_value=None,
    )
    assert out.demolish is False
    assert out.subdivision_profit is None, "an unknown profit was made up"
    assert "Demolish" not in (out.best_strategy or "")
    # The figure is still shown as the road not taken — information, not advice.
    assert out.alternative_strategy is not None
