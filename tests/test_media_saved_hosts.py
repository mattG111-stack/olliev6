import json
import media
from models import PropertyForSale
from tests.test_portal_review_flow import batch


def test_new_photo_host_after_legacy_prefix_is_allowed(db_session, monkeypatch):
    bid=batch(db_session).id
    db_session.bulk_insert_mappings(PropertyForSale, [
        {"import_batch_id":bid,"address": f"{i} Old Road", "image_url": "https://old.example.test/cover.jpg"}
        for i in range(4000)
    ])
    db_session.add(PropertyForSale(import_batch_id=bid,address="1 New Road", image_url="https://cdn.new-source.co.nz/cover.jpg"))
    db_session.commit()
    monkeypatch.setattr(media.settings, "media_hosts", "")
    monkeypatch.setattr(media, "_HOST_CACHE", None)
    assert media._host_allowed("https://cdn.new-source.co.nz/cover.jpg")
    assert not media._host_allowed("https://unrelated.invalid/photo.jpg")


def test_gallery_only_hosts_are_included_for_both_saved_formats(db_session):
    bid=batch(db_session).id
    db_session.add_all([
        PropertyForSale(import_batch_id=bid,address="1 Gallery Road", image_urls=json.dumps(["https://img.json-gallery.test/1.jpg", None])),
        PropertyForSale(import_batch_id=bid,address="2 Gallery Road", image_url="https://cover.example.test/1.jpg", image_urls="https://img.line-gallery.test/2.jpg\nhttps://img.line-gallery.test/3.jpg"),
        PropertyForSale(import_batch_id=bid,address="3 Gallery Road", image_urls="not a URL"),
    ])
    db_session.commit()
    hosts=media._hosts_from_our_own_listings()
    assert "json-gallery.test" in hosts and "line-gallery.test" in hosts
    assert "example.test" in hosts
    assert "not a URL" not in hosts


def test_repeated_saved_photos_are_parsed_once(db_session, monkeypatch):
    bid = batch(db_session).id
    cover = "https://cover.example.test/repeated.jpg"
    gallery = "https://gallery.other.test/repeated.jpg"
    db_session.add_all([PropertyForSale(import_batch_id=bid, address=f"{i} Repeat Road",
        image_url=cover, image_urls=json.dumps([cover, gallery])) for i in range(100)])
    db_session.commit()
    original = media.httpx.URL
    calls = []
    def parse(url):
        calls.append(url)
        return original(url)
    monkeypatch.setattr(media.httpx, "URL", parse)
    hosts = media._hosts_from_our_own_listings()
    assert "example.test" in hosts and "other.test" in hosts
    assert calls.count(cover) == 1
    assert calls.count(gallery) == 1
