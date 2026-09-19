"""Serve listing photographs from our own domain.

    "all those urls your importing you can clean up that protects the data
     source right as I can click on that I already knew what is where the
     photos were from"

Correct, and it did not even need a click. Every photograph on the site was
loaded straight from the portal that supplied it, so the browser's network tab
named the supplier on page load, and `image_urls` handed over the full gallery
path for every listing in one field. Anybody who opened the developer console —
a customer, a competitor, anyone — could read where the data comes from.

So the source URL never leaves the server. It is ENCRYPTED into an opaque token
(Fernet, keyed off the app's own secret) and the browser is given a path on our
domain instead:

    https://<the supplier's CDN>/…/photo.jpg?x-oss-process=…
        becomes
    /api/img/gAAAAABm…?w=420

The token is ciphertext, not an encoding — base64 of the URL would have been
decodable by anyone who looked, which is no protection at all. Only this server
holds the key, so only this server can say where the picture came from.

WIDTH SURVIVES THE MOVE. The frontend asks for a bigger copy by putting `?w=` on
the URL (see lib/img.ts), and most image CDNs encode the width in the URL, so
that rewrite has to happen on the SOURCE — which is now only visible here. The
proxy takes the width off its own query string and applies it to the real URL
before fetching. Without that, every photograph on the site quietly drops back
to the ~300px thumbnail the scrape stored.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import time

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/img", tags=["media"])

# WHICH HOSTS MAY BE FETCHED — and not one of them is written down here.
#
# The first version of this file listed them by name, in a tuple, in the
# repository. That is a list of who we buy from sitting in the code, which
# defeats the point of a proxy whose whole job is that nothing names them.
#
# So the allow-list is DERIVED FROM THE DATA. The hosts we are willing to fetch
# a photograph from are exactly the hosts our own listings already point at —
# a question the database can answer, and one that stays right on its own when
# a supplier changes or a new one is added. MEDIA_HOSTS overrides it for a
# deployment that wants to pin the list explicitly.
#
# The guard still matters: a proxy that fetches anything is a way to make our
# own server reach whatever the container can reach — cloud metadata included —
# so an empty answer refuses everything rather than allowing everything.
_HOST_CACHE: tuple[float, frozenset[str]] | None = None
_HOST_TTL = 600.0            # seconds; a supplier does not change in an hour


def _configured_hosts() -> frozenset[str]:
    raw = (settings.media_hosts or "").replace(";", ",")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _hosts_from_our_own_listings() -> frozenset[str]:
    """The hosts our stored photographs already come from."""
    from .db import SessionLocal
    from .models import PropertyForSale

    out: set[str] = set()
    db = SessionLocal()
    try:
        rows = (db.query(PropertyForSale.image_url)
                .filter(PropertyForSale.image_url.isnot(None))
                .limit(4000).all())
        for (u,) in rows:
            try:
                h = (httpx.URL(u).host or "").lower()
            except Exception:                          # noqa: BLE001
                continue
            if not h:
                continue
            # Registrable domain, so a CDN shard we have not seen before still
            # works: img7.example.com is allowed by example.com.
            parts = h.split(".")
            out.add(".".join(parts[-3:]) if len(parts) > 2 and parts[-2] in
                    ("co", "com", "net", "org") else ".".join(parts[-2:]))
    except Exception:                                  # noqa: BLE001
        log.exception("could not read the image hosts from the listings")
    finally:
        db.close()
    return frozenset(out)


def allowed_hosts() -> frozenset[str]:
    global _HOST_CACHE
    pinned = _configured_hosts()
    if pinned:
        return pinned
    now = time.monotonic()
    if _HOST_CACHE and now - _HOST_CACHE[0] < _HOST_TTL:
        return _HOST_CACHE[1]
    hosts = _hosts_from_our_own_listings()
    _HOST_CACHE = (now, hosts)
    return hosts

MAX_W = 1600
FETCH_TIMEOUT = 12.0
# Photographs do not change. A long cache keeps this off the critical path and
# keeps our own outbound traffic to the portals down.
CACHE_SECONDS = 60 * 60 * 24 * 30

_UA = ("Mozilla/5.0 (compatible; ApexPropertyBot/1.0; "
       "+listing image proxy)")


def _fernet() -> Fernet:
    """Same derivation as app/assistant/keys.py — one secret, one shape."""
    digest = hashlib.sha256(settings.jwt_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def tokenise(url: str | None) -> str | None:
    """A source URL as an opaque path on our own domain.

    Returns None for a blank input so a listing with no photograph stays a
    listing with no photograph rather than gaining a broken one.
    """
    if not url or not str(url).strip():
        return None
    try:
        tok = _fernet().encrypt(str(url).strip().encode()).decode()
    except Exception:                                   # noqa: BLE001
        return None
    return f"/api/img/{tok}"


def detokenise(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception):                   # noqa: BLE001
        return None


def _host_allowed(url: str) -> bool:
    try:
        u = httpx.URL(url)
        host = (u.host or "").lower()
    except Exception:                                   # noqa: BLE001
        return False
    if not host or u.scheme not in ("http", "https"):
        return False
    hosts = allowed_hosts()
    return any(host == h or host.endswith("." + h) for h in hosts)


def widen(url: str, want: int) -> str:
    """Ask the source CDN for a larger copy — the server-side twin of
    lib/img.ts:hiRes, which can no longer see the real URL to do it itself.

    Conservative in the same way: a URL is only rewritten when a size token we
    RECOGNISE is present, so an unfamiliar host is never handed a broken src.
    """
    want = max(1, min(int(want or 0), MAX_W))
    if not want:
        return url
    try:
        if re.search(r"x-oss-process=", url, re.I):
            return re.sub(r"([?&]x-oss-process=image/resize[^&]*?w_)(\d+)",
                          lambda m: m.group(1) + str(max(int(m.group(2)), want)),
                          url, flags=re.I)
        if re.search(r"[?&](w|width)=\d+", url, re.I):
            return re.sub(r"([?&](?:w|width)=)(\d+)",
                          lambda m: m.group(1) + str(max(int(m.group(2)), want)),
                          url, flags=re.I)
        m = re.search(r"/imageView2/\d/w/(\d+)", url, re.I)
        if m:
            return url.replace(m.group(0),
                               m.group(0).replace(m.group(1),
                                                  str(max(int(m.group(1)), want))))
    except Exception:                                   # noqa: BLE001
        return url
    return url


# ---- the outbound link ------------------------------------------------------
#
# The listing URL had the same problem as the photographs and one more besides:
# it is the thing a customer deliberately clicks, so it was the supplier's name
# handed over on purpose. It becomes a redirect we own — the customer still
# lands on the real advertisement, and the address only resolves at the moment
# they go, on our server, not in the payload sitting in their browser.
link_router = APIRouter(prefix="/api/go", tags=["media"])


def link(url: str | None) -> str | None:
    """A listing URL as a redirect through us."""
    if not url or not str(url).strip():
        return None
    try:
        tok = _fernet().encrypt(str(url).strip().encode()).decode()
    except Exception:                                   # noqa: BLE001
        return None
    return f"/api/go/{tok}"


@link_router.get("/{token}")
def follow(token: str):
    """Send the customer to the advertisement.

    Not behind the paywall for the same reason the images are not: a browser
    following a link does not carry our auth header. The token is the guard.
    """
    from fastapi.responses import RedirectResponse

    dest = detokenise(token)
    if not dest or not _host_allowed(dest):
        raise HTTPException(status_code=404, detail="No such listing")
    # 302, not 301: a permanent redirect is cached by the browser and by every
    # proxy in between, which would pin a listing's address in places we cannot
    # clear when it moves.
    return RedirectResponse(dest, status_code=302)


@router.get("/{token}")
def image(token: str, w: int = Query(0, ge=0, le=MAX_W)) -> Response:
    """Fetch and stream one photograph.

    Deliberately NOT behind the paywall. A browser loading an <img> does not
    send our auth header, so a guarded image endpoint shows every customer a
    page of broken pictures. The token is the protection: it is ciphertext only
    this server can read, and it names one photograph.
    """
    src = detokenise(token)
    if not src or not _host_allowed(src):
        raise HTTPException(status_code=404, detail="No such image")
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": _UA}) as c:
            r = c.get(widen(src, w))
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Image unavailable") from None
    if r.status_code != 200:
        raise HTTPException(status_code=404, detail="No such image")
    media = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not media.startswith("image/"):
        # The upstream answered with something that is not a picture — an error
        # page, a login wall. Serving it on our own domain would be worse than
        # serving nothing.
        raise HTTPException(status_code=404, detail="No such image")
    return Response(content=r.content, media_type=media,
                    headers={"Cache-Control": f"public, max-age={CACHE_SECONDS}, immutable"})
