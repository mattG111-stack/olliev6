from concurrent.futures import ThreadPoolExecutor
import threading
import time
import media


def test_simultaneous_images_share_one_host_scan(monkeypatch):
    monkeypatch.setattr(media.settings, 'media_hosts', '')
    monkeypatch.setattr(media, '_HOST_CACHE', None)
    calls=[]
    def scan():
        calls.append(1)
        time.sleep(.03)
        return frozenset({'example.test'})
    monkeypatch.setattr(media, '_hosts_from_our_own_listings', scan)
    gate=threading.Barrier(12)
    def request(_):
        gate.wait()
        return media.allowed_hosts()
    with ThreadPoolExecutor(max_workers=12) as pool:
        values=list(pool.map(request, range(12)))
    assert values==[frozenset({'example.test'})]*12
    assert len(calls)==1


def test_same_photo_reuses_cacheable_url_but_secret_rotation_does_not(monkeypatch):
    monkeypatch.setattr(media.settings,'jwt_secret','synthetic-first-key')
    url='https://images.example.test/photo.jpg'
    first=media.tokenise(url)
    assert media.tokenise(url)==first
    assert media.detokenise(first.rsplit('/',1)[1])==url
    monkeypatch.setattr(media.settings,'jwt_secret','synthetic-second-key')
    second=media.tokenise(url)
    assert second!=first
    assert media.detokenise(second.rsplit('/',1)[1])==url
    assert media.detokenise(first.rsplit('/',1)[1]) is None
