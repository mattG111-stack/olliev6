"""The two things that must travel with the mains wherever they are drawn.

CC BY 4.0 permits commercial use. It does not permit quiet use: credit, a link
to the licence, and a statement that the data was changed are conditions of the
grant, and we do change it — reprojected, filtered to the public network, and
measured against every parcel. Dropping the credit does not break a screen or
fail a build; it breaks the licence, silently, for as long as nobody checks.

And the warning, which is the one that matters on a building site. Every
operator publishes these positions as indicative. A developer who reads this
screen as a service locate and puts a digger through a water main was misled by
us. So it rides with the lines rather than living in a terms page nobody opens.

This is the single exception to not naming where our data comes from, and it is
narrow on purpose: a licence condition on one layer, not an account of how the
product works.
"""
from __future__ import annotations

import json

import pytest

from app.models import ServicePipe
from app.services_network import (CAUTION, LICENCE_URL, UNNAMED_OWNER,
                                  credit_line)

LAT, LNG = -36.9021, 174.8600


def _pipe(db, kind: str, owner: str | None) -> None:
    db.add(ServicePipe(
        region="Auckland", kind=kind, owner=owner,
        path=json.dumps([[LAT, LNG], [LAT + 0.0002, LNG + 0.0002]])))


def _services(db, lat: float = LAT, lng: float = LNG):
    from app.routers.geo import services

    return services(lat=lat, lng=lng, radius_m=250.0, region="Auckland",
                    db=db, user=None)


# ---- the licence -------------------------------------------------------------
def test_the_credit_names_the_operator_whose_pipes_are_drawn():
    assert "Watercare" in credit_line({"WATERCARE"})
    assert "Auckland Council" in credit_line({"STORMWATER"})


def test_a_screen_of_stormwater_does_not_credit_the_water_operator():
    """Crediting the wrong body is worse than a missing credit — it is a claim
    about someone else's data."""
    line = credit_line({"STORMWATER"})
    assert "Auckland Council" in line
    assert "Watercare" not in line


def test_both_operators_are_credited_when_both_are_on_screen():
    line = credit_line({"STORMWATER", "WATERCARE"})
    assert "Auckland Council" in line and "Watercare" in line


def test_an_unknown_owner_credits_somebody_rather_than_nobody():
    """The owner code is a business unit and the codes differ by region. An
    unrecognised one must not silently drop the attribution."""
    line = credit_line({"XYZ-UTILITIES"})
    assert UNNAMED_OWNER in line
    assert line.strip()


def test_the_credit_says_the_data_was_changed():
    """A condition of CC BY in its own right, and true: reprojected, filtered
    to the public network, and measured against every parcel."""
    assert "changes" in credit_line({"WATERCARE"}).lower()


def test_the_credit_names_the_licence():
    assert "CC BY 4.0" in credit_line({"WATERCARE"})


def test_the_licence_link_is_the_licence():
    assert LICENCE_URL.startswith("https://creativecommons.org/licenses/by/4.0")


# ---- the warning -------------------------------------------------------------
def test_the_warning_says_both_halves():
    """"Approximate" alone is not the warning. The instruction — confirm on
    site before digging — is the half that stops a main being hit."""
    low = CAUTION.lower()
    assert "approximate" in low or "indicative" in low
    assert "on site" in low
    assert "excavation" in low or "dig" in low


# ---- and they reach the screen ----------------------------------------------
def test_the_services_response_carries_the_credit_and_the_warning(db_session):
    _pipe(db_session, "water", "WATERCARE")
    db_session.commit()
    got = _services(db_session)
    assert got.lines, got
    assert "Watercare" in got.attribution
    assert got.licence_url == LICENCE_URL
    assert got.caution == CAUTION


def test_nothing_is_credited_when_nothing_is_drawn(db_session):
    """An empty panel makes no use of anybody's data, and a warning about
    pipes that are not on screen is noise that teaches people to skip it."""
    _pipe(db_session, "water", "WATERCARE")
    db_session.commit()
    got = _services(db_session, lat=-41.29, lng=174.78)
    assert got.lines == []
    assert got.attribution == ""
    assert got.caution == ""
