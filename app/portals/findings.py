"""Recording what a portal offered, and applying it once someone agrees.

The pass used to write as it went. It now records and waits, because a figure
scraped off someone else's page is a CLAIM, and the moment it lands in a priced
field it becomes indistinguishable from data we stand behind. The person who has
to defend a valuation should have seen the number that moved it.

So: the job produces findings, a person looks at them, and only then is anything
written — through the same pricing and the same hold rules as every other change.

A rejected finding is kept rather than deleted. Next week the same portal will
offer the same wrong number, and a record of it having been refused is the
difference between deciding once and deciding every week.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import PortalFinding, PropertyForSale
from . import ESTIMATE_COLUMNS, PortalResult
from .fill import BOUNDS, FACTS, PRICED_FIELDS, _missing, _sane

log = logging.getLogger(__name__)


def _text(v) -> str | None:
    return None if v is None else str(v)[:255]


def _already_offered(db: Session, property_id: int, source: str, field: str,
                     value_num, value_text) -> bool:
    """Has this exact claim been recorded — or refused — before?

    Stops a weekly run from re-asking a question that has already been answered,
    and stops a pending list from growing a duplicate every time the button is
    pressed.
    """
    q = (db.query(PortalFinding)
         .filter(PortalFinding.property_id == property_id,
                 PortalFinding.source == source,
                 PortalFinding.field == field))
    for row in q.all():
        if value_num is not None and row.value_num is not None:
            if abs(row.value_num - float(value_num)) < 0.001:
                return True
        elif value_text is not None and row.value_text == _text(value_text):
            return True
    return False


def record(db: Session, prop: PropertyForSale, res: PortalResult,
           *, batch_id: int | None = None) -> list[PortalFinding]:
    """Write down what this portal said about this property. Changes nothing."""
    out: list[PortalFinding] = []
    if res is None:
        return out

    def add(field: str, kind: str, value, extra: dict | None = None) -> None:
        num = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        text = None if num is not None else value
        if _already_offered(db, prop.id, res.source, field, num, text):
            return
        current = getattr(prop, field, None)
        f = PortalFinding(
            property_id=prop.id, batch_id=batch_id, source=res.source,
            field=field, kind=kind,
            value_num=float(num) if num is not None else None,
            value_text=_text(text),
            current_num=float(current) if isinstance(current, (int, float))
            and not isinstance(current, bool) else None,
            current_text=_text(current) if not isinstance(current, (int, float)) else None,
            extra_json=json.dumps(extra) if extra else None,
            status="pending",
        )
        db.add(f)
        out.append(f)

    # Facts — offered only where we hold nothing, and only if the number could
    # plausibly be the thing it claims to be.
    for src, column in FACTS.items():
        value = getattr(res, src, None)
        if _missing(value) or not hasattr(prop, column):
            continue
        if not _missing(getattr(prop, column)):
            continue
        if not _sane(column, value):
            continue
        add(column, "fact" if column in PRICED_FIELDS else "detail", value)

    # Their estimate — always offered, because it is a current figure and moves.
    cols = ESTIMATE_COLUMNS.get(res.source)
    if cols and res.estimate is not None and _sane("estimate", res.estimate):
        mid, low, high, url = cols
        add(mid, "estimate", res.estimate, extra={
            "low": res.estimate_low, "high": res.estimate_high,
            "url": res.url, "low_col": low, "high_col": high, "url_col": url,
        })

    if out:
        db.commit()
    return out


def approve(db: Session, finding_id: int, *, user_id: int | None = None,
            reprice=None) -> tuple[bool, str]:
    """Write one finding and re-price the listing if it changed the value.

    Returns (applied, why-not). A finding is refused rather than written when
    the property has since gained a value of its own — the point of a fill is a
    blank field, and between the lookup and the decision the real data may have
    arrived.
    """
    f = db.get(PortalFinding, finding_id)
    if f is None:
        return False, "no such finding"
    if f.status != "pending":
        return False, f"already {f.status}"

    prop = db.get(PropertyForSale, f.property_id)
    if prop is None:
        f.status = "rejected"
        f.decided_at = datetime.now(timezone.utc)
        db.commit()
        return False, "the listing is gone"

    value = f.value_num if f.value_num is not None else f.value_text

    if f.kind == "estimate":
        extra = json.loads(f.extra_json) if f.extra_json else {}
        setattr(prop, f.field, float(f.value_num))
        for key, col in (("low", "low_col"), ("high", "high_col")):
            column, val = extra.get(col), extra.get(key)
            if column and val is not None and hasattr(prop, column):
                setattr(prop, column, float(val))
        column, val = extra.get("url_col"), extra.get("url")
        if column and val and hasattr(prop, column) and _missing(getattr(prop, column)):
            setattr(prop, column, str(val)[:300])
    else:
        if not _missing(getattr(prop, f.field, None)):
            f.status = "rejected"
            f.decided_at = datetime.now(timezone.utc)
            f.decided_by_id = user_id
            db.commit()
            return False, "we have our own value for that now"
        if not _sane(f.field, value):
            f.status = "rejected"
            f.decided_at = datetime.now(timezone.utc)
            db.commit()
            return False, "outside the plausible range for that field"
        setattr(prop, f.field, value)

    f.status = "applied"
    f.decided_at = datetime.now(timezone.utc)
    f.decided_by_id = user_id
    db.commit()

    # A fact changes what the property is worth, so it goes back through the
    # same pricing and the same hold rules as any other change. An estimate is
    # that portal's opinion and moves nothing of ours.
    if f.kind == "fact":
        fn = reprice
        if fn is None:
            from ..reprice import reprice_one as fn
        try:
            fn(db, prop.id)
        except ValueError:
            pass                                   # nothing to price against yet
        except Exception as e:                     # noqa: BLE001
            log.info("re-price after approving %s failed: %s", finding_id, e)
    return True, "applied"


def reject(db: Session, finding_id: int, *, user_id: int | None = None) -> bool:
    """Refuse a finding, and keep it. Next week the same portal offers the same
    number, and a record of the decision is what stops it being made twice."""
    f = db.get(PortalFinding, finding_id)
    if f is None or f.status != "pending":
        return False
    f.status = "rejected"
    f.decided_at = datetime.now(timezone.utc)
    f.decided_by_id = user_id
    db.commit()
    return True
