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

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PageView, User
from ..security import current_user

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


@router.post("/page", status_code=204)
def record_page_view(body: PageViewIn,
                     me: User = Depends(current_user),
                     db: Session = Depends(get_db)) -> None:
    """Record one page visit. Never raises; a failure here is not worth a 500."""
    try:
        path = normalise_path(body.path)
        if not path:
            return
        seconds = body.seconds
        if seconds is not None and (seconds < 0 or seconds > MAX_DWELL_SECONDS):
            seconds = None
        db.add(PageView(user_id=me.id if me else None, path=path, seconds=seconds))
        db.commit()
    except Exception:
        db.rollback()
    return
