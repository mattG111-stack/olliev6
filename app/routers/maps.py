"""Where the aerial imagery comes from, set once by an admin.

These were NEXT_PUBLIC_ build-time variables baked into the frontend bundle.
Three problems with that, all of which cost real time:

  1. Changing or rotating a key meant a rebuild and a full redeploy of the site.
  2. A key set without ALSO setting the provider name silently did nothing,
     which looks exactly like a key that does not work.
  3. A NEXT_PUBLIC_ variable is compiled into public JavaScript, so the key was
     downloadable by anyone who loaded the site, signed in or not.

Served from here, the key lives in the database, an admin changes it in the
panel, it takes effect on the next page load, and the config endpoint sits
behind the same paywall as the listings — so only someone entitled to see a
property can get the key that draws its photo. It still reaches the browser,
because the browser is what talks to Google; a domain restriction on the key is
still the thing that stops someone else spending it, and the admin page says so.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import settings_store
from ..assistant import keys
from ..db import get_db
from ..models import (
    MAPS_GOOGLE_KEY,
    MAPS_LINZ_KEY,
    MAPS_PROVIDER,
    AppSetting,
    User,
)
from ..security import require_admin

log = logging.getLogger(__name__)

# Mounted under the paywall in main.py, alongside the listing data itself.
router = APIRouter(prefix="/api/config", tags=["config"])
admin_router = APIRouter(prefix="/api/admin/maps", tags=["admin"])

PROVIDERS = ("google", "linz", "esri")


def _key(db: Session, setting: str) -> str | None:
    """The plaintext key, or None if there is not a readable one.

    Fails closed the same way the assistant key does: a value encrypted under a
    different JWT secret reads as absent, so imagery drops to the free layer
    rather than sending a broken key to Google and drawing nothing.
    """
    return keys.decrypt(settings_store.get(db, setting))


def _resolve(provider: str | None, google: str | None, linz: str | None) -> str:
    """Which source to actually use.

    A named provider only wins if its key is present. Naming one whose key is
    missing would turn every map into a grey box, and falling through to
    something that draws beats being right about the configuration.
    """
    p = (provider or "").strip().lower()
    if p == "google" and google:
        return "google"
    if p == "linz" and linz:
        return "linz"
    if p == "esri":
        return "esri"
    if google:
        return "google"
    if linz:
        return "linz"
    return "esri"


class MapConfig(BaseModel):
    """What the browser needs to draw an aerial. Only the resolved provider's
    key is returned — sending both would hand out a credential the page has no
    use for."""
    provider: str
    google_key: str | None = None
    linz_key: str | None = None


@router.get("/maps", response_model=MapConfig)
def map_config(db: Session = Depends(get_db)) -> MapConfig:
    settings_store.ensure_table()
    google = _key(db, MAPS_GOOGLE_KEY)
    linz = _key(db, MAPS_LINZ_KEY)
    provider = _resolve(settings_store.get(db, MAPS_PROVIDER), google, linz)
    return MapConfig(
        provider=provider,
        google_key=google if provider == "google" else None,
        linz_key=linz if provider == "linz" else None,
    )


# ---- admin ------------------------------------------------------------------

class MapsStatus(BaseModel):
    provider: str
    provider_setting: str | None = None
    google_configured: bool
    google_last_four: str | None = None
    linz_configured: bool
    linz_last_four: str | None = None
    updated_at: datetime | None = None
    detail: str


class MapsIn(BaseModel):
    """Patch semantics on purpose.

    A field left out is left alone, so saving the provider does not require
    re-typing a key nobody can read back. An EMPTY STRING clears that key —
    there has to be a way to remove one, and a separate delete endpoint per key
    is more surface for the same thing.
    """
    provider: str | None = None
    google_key: str | None = Field(default=None, max_length=200)
    linz_key: str | None = Field(default=None, max_length=200)


def _explain(exc: Exception) -> HTTPException:
    log.exception("admin maps endpoint failed")
    return HTTPException(
        status_code=503,
        detail=f"Map settings are unavailable: {type(exc).__name__}: {exc}",
    )


def _status(db: Session) -> MapsStatus:
    g_stored = settings_store.get(db, MAPS_GOOGLE_KEY)
    l_stored = settings_store.get(db, MAPS_LINZ_KEY)
    google, linz = keys.decrypt(g_stored), keys.decrypt(l_stored)
    setting = (settings_store.get(db, MAPS_PROVIDER) or "").strip().lower() or None
    provider = _resolve(setting, google, linz)

    if provider == "google":
        detail = ("Google satellite. Map Tiles API where a tile session can be "
                  "minted, Maps Static API if it cannot — both need to be enabled "
                  "on the key.")
    elif provider == "linz":
        detail = "LINZ Basemaps aerial — 10-30 cm over New Zealand, free with the key."
    elif g_stored and not google:
        detail = ("A Google key is saved but cannot be read with the current "
                  "server secret. Enter it again to replace it. Showing Esri "
                  "until then.")
    else:
        detail = ("No key set — showing the free Esri layer, which has no imagery "
                  "past zoom 19 and is enlarged beyond that.")

    rows = [db.get(AppSetting, k) for k in (MAPS_PROVIDER, MAPS_GOOGLE_KEY, MAPS_LINZ_KEY)]
    stamps = [r.updated_at for r in rows if r is not None and r.updated_at is not None]

    return MapsStatus(
        provider=provider,
        provider_setting=setting,
        google_configured=bool(google),
        google_last_four=keys.last_four(g_stored),
        linz_configured=bool(linz),
        linz_last_four=keys.last_four(l_stored),
        updated_at=max(stamps) if stamps else None,
        detail=detail,
    )


@admin_router.get("", response_model=MapsStatus)
def get_maps(_: User = Depends(require_admin),
             db: Session = Depends(get_db)) -> MapsStatus:
    settings_store.ensure_table()
    try:
        return _status(db)
    except Exception as exc:
        raise _explain(exc) from exc


@admin_router.put("", response_model=MapsStatus)
def set_maps(body: MapsIn, me: User = Depends(require_admin),
             db: Session = Depends(get_db)) -> MapsStatus:
    """Save whichever fields were sent.

    The keys are NOT verified against the provider before saving, unlike the
    assistant key. There is no free way to check a maps key — every call that
    would prove it works is a billable request, and a wrong key fails visibly
    on the very next map anyway. What is checked is the shape, because the
    common paste error is an obvious one.
    """
    settings_store.ensure_table()

    if body.provider is not None:
        p = body.provider.strip().lower()
        if p and p not in PROVIDERS:
            raise HTTPException(
                status_code=422,
                detail=f"Provider must be one of {', '.join(PROVIDERS)} (or blank to choose automatically).")

    if body.google_key:
        g = body.google_key.strip()
        # Browser keys are "AIza" + 35 more characters. An OAuth client id or a
        # whole URL pasted in by mistake is caught here rather than by every
        # property page silently losing its photo.
        if not g.startswith("AIza") or len(g) < 30:
            raise HTTPException(
                status_code=422,
                detail="That does not look like a Google Maps API key — they start "
                       "with AIza and are about 39 characters. Copy it from "
                       "Google Cloud → APIs & Services → Credentials.")

    if body.linz_key and len(body.linz_key.strip()) < 20:
        raise HTTPException(
            status_code=422,
            detail="That does not look like a LINZ Basemaps API key — they are "
                   "roughly 35 characters and start with c.")

    try:
        if body.provider is not None:
            settings_store.put(db, MAPS_PROVIDER, body.provider.strip().lower() or None, by=me.id)
        for field, setting in ((body.google_key, MAPS_GOOGLE_KEY), (body.linz_key, MAPS_LINZ_KEY)):
            if field is None:
                continue
            value = field.strip()
            settings_store.put(db, setting, keys.encrypt(value) if value else None, by=me.id)
    except Exception as exc:
        raise _explain(exc) from exc

    status = _status(db)
    # The key itself never goes near a log line; which one is in use does.
    log.warning("map imagery set by %s (provider=%s, google=%s, linz=%s)",
                me.email, status.provider, status.google_configured, status.linz_configured)
    return status
