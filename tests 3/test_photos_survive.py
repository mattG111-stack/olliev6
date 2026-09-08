"""A portal listing should arrive with its photos, not with one of them.

    "are we getting the same files as the up loaded data ? like the photos ?"

No, and the reason was one word. The actors return a LIST of photos; the parser
called _first_url on it and kept the first. Everything after it was discarded at
the moment it arrived, so an approved portal listing had a single picture where
a listing from the weekly file has up to thirty.

That matters more on these rows than anywhere else. They are the newest
properties in the system — the whole point of the sweep is that they are not in
the file yet — and a property page with one photo does not read as a fresh
listing, it reads as a broken one.

Stored the same way the weekly file stores it: newline-separated in image_urls
with image_count beside it, exactly what ingest._collect_images produces from
image_1_url .. image_30_url. The property page then needs to know nothing about
where a row came from.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")

import pytest  # noqa: E402

from app.portals.listings import _CARRIED, _CARRIED_SOLD, _gallery  # noqa: E402
from app.portals.sources import _all_urls, _first_url  # noqa: E402


def test_every_photo_is_kept_not_just_the_first():
    urls = ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"]
    joined, n = _gallery(urls)

    assert n == 3
    assert joined.split("\n") == urls


def test_the_portals_own_order_is_kept():
    """The first photo is the one the portal leads with. Re-ordering would put
    a bathroom on the card."""
    urls = ["https://a/hero.jpg", "https://a/kitchen.jpg", "https://a/bath.jpg"]
    assert _gallery(urls)[0].split("\n")[0] == "https://a/hero.jpg"
    assert _first_url(urls) == "https://a/hero.jpg"


def test_photos_arrive_as_objects_too():
    """One actor returns strings, another returns {url: ...} — both are photos."""
    items = [{"url": "https://a/1.jpg"}, {"base_url": "https://a/2.jpg"},
             "https://a/3.jpg"]
    assert _all_urls(items) == ["https://a/1.jpg", "https://a/2.jpg",
                                "https://a/3.jpg"]


def test_the_same_photo_twice_is_shown_once():
    """A gallery that repeats itself looks broken."""
    urls = ["https://a/1.jpg", "https://a/1.jpg", "https://a/2.jpg"]
    assert _gallery(urls)[1] == 2


@pytest.mark.parametrize("empty", [None, [], "", "   ", [""], [{}], [None]])
def test_no_photos_is_none_rather_than_an_empty_string(empty):
    """An empty string in image_urls would render as one broken image."""
    assert _gallery(empty) == (None, None)


def test_a_single_photo_as_a_bare_string_still_works():
    joined, n = _gallery("https://a/only.jpg")
    assert (joined, n) == ("https://a/only.jpg", 1)


# ---- and it has to survive approval -----------------------------------------
def test_the_gallery_is_carried_onto_the_approved_listing():
    """Capturing them and then dropping them at the door would be the same bug
    one step later — which is exactly what happened to eleven other fields."""
    for field in ("image_url", "image_urls", "image_count"):
        assert field in _CARRIED, f"{field} is dropped when a listing is approved"


def test_a_sold_record_keeps_its_photos_too():
    assert "image_urls" in _CARRIED_SOLD and "image_count" in _CARRIED_SOLD


def test_there_is_only_one_definition_of_first_url():
    """There were two, in two modules, which is how a rule starts disagreeing
    with itself."""
    import glob
    import re

    defs = [f for f in glob.glob("app/**/*.py", recursive=True)
            if re.search(r"^def _first_url", open(f).read(), re.M)]
    assert defs == ["app/portals/sources.py"], defs
