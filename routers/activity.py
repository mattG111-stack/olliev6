"""Which features get used, and for how long.

One POST per page visit, sent when the visitor leaves the page so the row
carries its own dwell time. Deliberately the smallest thing that answers the
question — no session stitching, no funnels, no per-property ids.

Two rules this module exists to keep:

  * it can never break a page. Analytics is the least important request the app
    makes and it runs on every navigation, so every failure path returns quietly.
    A tracker that 500s takes the feature it was measuring down with it.
  * it never stores which PROPERTY someone looked at. The route is recorded, so
    "/property/8213" is stored as "/property". Knowing that listings get opened
    is the question; keeping a per-user record of which houses a customer viewed
    is a different thing entirely, and not one anybody asked for.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import get_db
from models import PageView, User
from security import current_user

router = APIRouter(prefix="/api/activity", tags=["activity"])

# Anything that identifies a single record is dropped from the route: numeric
# ids, and the long slug ids the scraper uses.
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{8,}|[a-z0-9]+(?:-[a-z0-9]+){3,})$", re.I)

# A tab left open overnight is not four hours of engagement. Anything longer
# than this is recorded as a visit with no duration rather than a lie.
MAX_DWELL_SECONDS = 30 * 60


def normalise_path(raw: str | None) -> str | None:
    """'/property/8213' -> '/property'. Returns None for anything unusable."""
    if not raw:
        return None
    p = str(raw).split("?")[0].split("#")[0].strip()
    if not p.startswith("/"):
        return None
    parts = [seg for seg in p.split("/") if seg]
    kept = [seg for seg in parts if not _ID_SEGMENT.match(seg)]
    out = "/" + "/".join(kept) if kept else "/"
    return out[:128]


class PageViewIn(BaseModel):
    path: str = Field(max_length=512)
    # Time the page was open. Absent when the visitor closed the tab before it
    # could be measured, which is normal and not an error.
    seconds: float | None = None


@router.post("/page", status_code=204, response_class=Response)
def record_page_view(body: PageViewIn,
                     me: User = Depends(current_user),
                     db: Session = Depends(get_db)) -> Response:
    """Record one page visit. Never raises; a failure here is not worth a 500.

    Returns an explicit empty Response rather than None. FastAPI 0.115 — the
    pinned version — treats a `-> None` annotation on a 204 route as a response
    model and refuses to build the app at all:

        AssertionError: Status code 204 must not have a response body

    That is an import-time failure, so the whole API stops booting, not just
    this endpoint. Newer FastAPI accepts it, which is exactly how it reached
    production: it imported cleanly against the version installed here and
    crash-looped against the one in requirements.txt.
    """
    try:
        path = normalise_path(body.path)
        if not path:
            return Response(status_code=204)
        seconds = body.seconds
        if seconds is not None and (seconds < 0 or seconds > MAX_DWELL_SECONDS):
            seconds = None
        db.add(PageView(user_id=me.id if me else None, path=path, seconds=seconds))
        db.commit()
    except Exception:
        db.rollback()
    return Response(status_code=204)


# Optional property memory has its own consent and per-user data boundary.
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from interest_memory import InterestMemorySetting, PropertyInterest, memory_summary
from security import require_active
from models import PropertyForSale


class MemorySettingIn(BaseModel):
    enabled: bool


class PropertyViewIn(BaseModel):
    property_id: int = Field(gt=0)


@router.get('/interests')
def get_interests(me: User = Depends(require_active), db: Session = Depends(get_db)):
    return memory_summary(db, me.id)


@router.put('/interests')
def set_interests(body: MemorySettingIn, me: User = Depends(require_active), db: Session = Depends(get_db)):
    setting = db.query(InterestMemorySetting).filter_by(user_id=me.id).with_for_update().first()
    if setting is None:
        setting = InterestMemorySetting(user_id=me.id)
        db.add(setting)
    setting.enabled = body.enabled
    if not body.enabled:
        db.query(PropertyInterest).filter_by(user_id=me.id).delete()
    db.commit()
    return memory_summary(db, me.id)


@router.delete('/interests', status_code=204, response_class=Response)
def clear_interests(me: User = Depends(require_active), db: Session = Depends(get_db)):
    # Serialize against incoming views, so a view cannot restore cleared data mid-delete.
    db.query(InterestMemorySetting).filter_by(user_id=me.id).with_for_update().first()
    db.query(PropertyInterest).filter_by(user_id=me.id).delete()
    db.commit()
    return Response(status_code=204)


@router.post('/property', status_code=204, response_class=Response)
def record_property_interest(body: PropertyViewIn, me: User = Depends(require_active), db: Session = Depends(get_db)):
    setting = db.query(InterestMemorySetting).filter_by(user_id=me.id).with_for_update().first()
    if not setting or not setting.enabled:
        return Response(status_code=204)
    from routers.properties import _active_batch, _hide_bad_data
    batch = _active_batch(db, 'for_sale', 'Auckland')
    p = _hide_bad_data(db.query(PropertyForSale).filter(
        PropertyForSale.id == body.property_id, PropertyForSale.import_batch_id == batch)).first() if batch else None
    if p is None:
        raise HTTPException(status_code=404, detail='Property unavailable')
    now = datetime.now(timezone.utc)
    row = db.get(PropertyInterest, (me.id, p.id))
    if row is None:
        row = PropertyInterest(user_id=me.id, property_id=p.id)
        db.add(row)
    row.suburb, row.last_viewed = p.suburb, now
    db.flush()
    db.query(PropertyInterest).filter(PropertyInterest.user_id == me.id,
        PropertyInterest.last_viewed < now - timedelta(days=30)).delete()
    # Bounded history; repeated views of the same record do not inflate interest.
    old_ids = [r.property_id for r in db.query(PropertyInterest).filter_by(user_id=me.id)
               .order_by(PropertyInterest.last_viewed.desc(), PropertyInterest.property_id.desc()).offset(100)]
    if old_ids:
        db.query(PropertyInterest).filter(PropertyInterest.user_id == me.id,
                                         PropertyInterest.property_id.in_(old_ids)).delete()
    db.commit()
    return Response(status_code=204)
