"""The browser must never learn where the data comes from.

    "all those urls your importing you can clean up that protects the data
     source right as I can click on that I already knew what is where the
     photos were from etc all from embedded links"

It did not even take a click. Every photograph loaded straight from the portal
that supplied it, so the network tab named the supplier on page load;
`image_urls` handed over the whole gallery path per listing in one field; and
next.config.js listed the portal hostnames in a file the browser fetches.

Three leaks, one fix: the source URL never leaves the server. It is encrypted
into a token only this server can read, and the browser is given a path on our
own domain. Base64 would not have done — an encoding anybody can reverse is not
a protection — so it is ciphertext.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app import media
from app.ingest import ingest_for_sale, ingest_sold
from app.models import BatchType, ImportBatch, PropertyForSale
from app.routers.properties import ForSaleRow

SRC = "https://s.example-cdn.com/gallery/abc123.jpg?x-oss-process=image/resize,w_300"
LISTING = "https://www.example-portal.co.nz/property/auckland/remuera/12345"


def _sold():
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Remuera", "district": "Auckland City",
        "region": "Auckland", "property_type": "House", "key_bedrooms": 3,
        "key_bathrooms": 1, "key_floor_area": f"{130 + i} sqm",
        "key_land_area": f"{700 + i * 10} sqm", "cv_numeric": 1_500_000,
        "price_numeric": 1_550_000 + i * 10_000, "sale_price": 1_550_000 + i * 10_000,
        "land_value_numeric": 1_000_000, "improvement_value_numeric": 500_000,
        "type_of_title": "Freehold", "sold_date": "2026-06-01",
    } for i in range(12)])


def _listing_row():
    return dict(address="1 Source Street", suburb="Remuera", district="Auckland City",
                region="Auckland", property_type="House", slug_id="1-source-street",
                url=LISTING,
                image_1_url=SRC,
                image_2_url=SRC.replace("abc123", "def456"),
                price_display="$1,600,000", price_numeric=1_600_000,
                sale_method="fixed price", cv_numeric=1_500_000,
                land_value_numeric=1_000_000, improvement_value_numeric=500_000,
                key_bedrooms=3, key_bathrooms=1, key_floor_area=140,
                key_land_area=750, type_of_title="Freehold")


# ---- the token is ciphertext, not an encoding -------------------------------
def test_the_source_cannot_be_read_out_of_the_token():
    """THE ONE THAT MATTERS. base64 of the URL would look opaque and protect
    nothing — anyone who noticed could decode it in a browser console."""
    tok = media.tokenise(SRC)
    assert tok and tok.startswith("/api/img/")
    body = tok.split("/api/img/")[1]
    assert "hougarden" not in body.lower()
    import base64
    for pad in ("", "=", "==", "==="):
        try:
            decoded = base64.urlsafe_b64decode(body + pad).decode("utf-8", "ignore")
        except Exception:                              # noqa: BLE001
            continue
        assert "hougarden" not in decoded.lower(), "the URL is merely encoded"


def test_only_this_server_can_read_it_back():
    assert media.detokenise(media.tokenise(SRC).split("/api/img/")[1]) == SRC


def test_a_token_we_did_not_write_is_refused():
    assert media.detokenise("not-a-real-token") is None


# ---- what actually goes over the wire ---------------------------------------
def test_no_listing_field_names_the_supplier(db_session):
    """Asked of the SERIALISED row — the actual JSON a browser receives."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=True)
    ingest_for_sale(db_session, pd.DataFrame([_listing_row()]), _sold(),
                    "live.csv", region="Auckland", publish=True)
    b = (db_session.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    p = (db_session.query(PropertyForSale)
         .filter(PropertyForSale.import_batch_id == b.id).one())

    wire = ForSaleRow.model_validate(p).model_dump_json().lower()
    # No supplier's ADDRESS, anywhere.
    for host in ("hougarden.com", "oneroof.co.nz", "trademe.co.nz",
                 "realestate.co.nz", "homes.co.nz", "propertyvalue.co.nz"):
        assert host not in wire, f"{host} is named in the payload"
    # And no supplier's NAME in the field names either — which is how the
    # first version of this test failed, on keys like oneroof_valuation.
    for name in ("oneroof", "hougarden", "hg_valuation"):
        assert name not in wire, f"'{name}' appears as a field name"


def test_the_photographs_still_have_somewhere_to_come_from(db_session):
    """Hiding them must not lose them."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=True)
    ingest_for_sale(db_session, pd.DataFrame([_listing_row()]), _sold(),
                    "live.csv", region="Auckland", publish=True)
    p = (db_session.query(PropertyForSale)
         .order_by(PropertyForSale.id.desc()).first())
    # Asked of the SERIALISED form, which is what a browser receives. Reading
    # the attribute gives the raw URL — field serialisers run on the way out,
    # and server-side code is entitled to see the real address.
    row = ForSaleRow.model_validate(p).model_dump()
    assert row["image_url"] and row["image_url"].startswith("/api/img/")
    assert row["url"] and row["url"].startswith("/api/go/")
    assert len([u for u in (row["image_urls"] or "").split("\n") if u]) == 2


# ---- the model's own workings are not published -----------------------------
def test_the_diagnostics_are_no_longer_sent_to_customers():
    """Seven fields describing which branch ran, what it anchored to and which
    correction was applied. No page read one of them."""
    for f in ("pred_v35", "pred_v38", "z_weight", "beta_tier", "cv_anchor",
              "cv_ratio_tier", "correction_used"):
        assert f not in ForSaleRow.model_fields, f"{f} is still published"


def test_the_operator_keeps_them():
    """They are diagnostics, and an operator reviewing a batch still needs
    them — on the admin response, which is where they belong."""
    from app.routers.release import StagedGridRow

    assert "confidence" in StagedGridRow.model_fields
    assert "deal_block_reason" in StagedGridRow.model_fields


# ---- the proxy is not an open fetcher ---------------------------------------
@pytest.mark.parametrize("bad", [
    "https://evil.example.com/x.jpg",
    "http://169.254.169.254/latest/meta-data/",       # cloud metadata
    "file:///etc/passwd",
])
def test_it_will_only_fetch_the_hosts_we_named(bad):
    """A proxy that fetches anything is a way to make our own server attack
    somebody else's, and a route to whatever the container can reach."""
    assert media._host_allowed(bad) is False


def test_the_allow_list_is_not_written_down_in_the_code(monkeypatch):
    """The first version of the proxy listed the suppliers by name, in a tuple,
    in the repository — a list of who we buy from, defeating the point of a
    proxy whose whole job is that nothing names them.

    It is configuration now, and unset it is worked out from our own listings.
    """
    from pathlib import Path

    src = Path("app/media.py").read_text().lower()
    for name in ("hougarden", "oneroof", "trademe", "realestate.co.nz",
                 "homes.co.nz", "propertyvalue"):
        assert name not in src, f"{name} is named in the proxy's source"


def test_a_configured_host_is_fetched(monkeypatch):
    monkeypatch.setattr(media.settings, "media_hosts",
                        "example-cdn.com, example-portal.co.nz")
    assert media._host_allowed("https://s.example-cdn.com/a.jpg") is True
    assert media._host_allowed("https://www.example-portal.co.nz/x") is True
    assert media._host_allowed("https://somewhere-else.com/a.jpg") is False


def test_with_nothing_configured_and_nothing_stored_it_refuses(monkeypatch, db_session):
    """Fails CLOSED. An empty answer must refuse everything, not allow
    everything — the opposite would turn this into a general-purpose fetcher."""
    monkeypatch.setattr(media.settings, "media_hosts", "")
    monkeypatch.setattr(media, "_HOST_CACHE", None)
    monkeypatch.setattr(media, "_hosts_from_our_own_listings", frozenset)
    assert media._host_allowed("https://s.example-cdn.com/a.jpg") is False


def test_it_learns_the_hosts_from_our_own_listings(monkeypatch):
    """No configuration, no hard-coded list: the hosts we will fetch from are
    the hosts our own photographs already come from."""
    monkeypatch.setattr(media.settings, "media_hosts", "")
    monkeypatch.setattr(media, "_HOST_CACHE", None)
    monkeypatch.setattr(media, "_hosts_from_our_own_listings",
                        lambda: frozenset({"example-cdn.com"}))
    assert media._host_allowed("https://img7.example-cdn.com/a.jpg") is True
    assert media._host_allowed("https://not-ours.com/a.jpg") is False


# ---- the picture is still the right size ------------------------------------
def test_the_width_survives_the_move():
    """The frontend can no longer see the real URL to ask the CDN for a bigger
    copy, so the proxy has to do it — or every photo on the site quietly drops
    back to the ~300px thumbnail the scrape stored."""
    assert "w_800" in media.widen(SRC, 800)
    assert "w_300" not in media.widen(SRC, 800)


def test_a_width_smaller_than_the_original_is_left_alone():
    assert "w_300" in media.widen(SRC, 100)


def test_an_unfamiliar_url_is_never_mangled():
    plain = "https://s.hougarden.com/plain.jpg"
    assert media.widen(plain, 900) == plain
