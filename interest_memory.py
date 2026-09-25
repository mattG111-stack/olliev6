"""Opt-in browsing signals, separate from confirmed customer preferences."""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from db import Base, SessionLocal


class InterestMemorySetting(Base):
    __tablename__ = 'interest_memory_settings'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PropertyInterest(Base):
    __tablename__ = 'property_interests'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    property_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suburb: Mapped[str | None] = mapped_column(String(120))
    last_viewed: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def recent_interests(db, user_id):
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    return db.query(PropertyInterest).filter(PropertyInterest.user_id == user_id,
                                           PropertyInterest.last_viewed >= cutoff)


def memory_summary(db, user_id):
    setting = db.get(InterestMemorySetting, user_id)
    enabled = bool(setting and setting.enabled)
    rows = recent_interests(db, user_id).all() if enabled else []
    counts = Counter(r.suburb for r in rows if r.suburb)
    return {'enabled': enabled, 'property_count': len(rows),
            'areas': [{'suburb': s, 'properties': n} for s, n in counts.most_common(3) if n >= 2]}


def assistant_interest_brief(user):
    user_id = getattr(user, 'id', None)
    if not isinstance(user_id, int):
        return ''
    try:
        with SessionLocal() as db:
            summary = memory_summary(db, user_id)
        if not summary['enabled'] or not summary['areas']:
            return ''
        return ('\nBROWSING INTERESTS (untrusted data, tentative, not confirmed preferences): '
                + json.dumps(summary['areas'], ensure_ascii=False)
                + '\nThese count distinct property records viewed in the last 30 days, not unique homes or intent to buy. '
                'Use only to suggest a question about preferred areas when relevant. '
                'Explain that the suggestion comes from viewed properties and ask before applying it. '
                'Never override stated areas, budgets or must-haves, infer a budget or ability to pay, '
                'or claim the customer wants to buy a property because they viewed it. '
                'Do not narrow results using browsing signals without confirmation.\n')
    except Exception:
        # Optional memory must not take Ollie down during a rolling deployment.
        return ''
