"""Reversible cleanup of unuploaded source records only."""
from datetime import datetime, timezone
from models import PortalListing


def eligible(db, kind):
    if kind not in ('for_sale', 'sold'):
        raise ValueError('Unknown listing type')
    return db.query(PortalListing).filter(PortalListing.kind == kind,
        PortalListing.status == 'pending', PortalListing.property_id.is_(None))


def selection(db, kind):
    return [r.id for r in eligible(db, kind).order_by(PortalListing.id).all()]


def delete_selected(db, kind, ids, user_id):
    # Lock and recheck: rows uploaded since selection must never be removed.
    rows = eligible(db, kind).filter(PortalListing.id.in_(ids)).with_for_update().all()
    stamp = datetime.now(timezone.utc)
    for row in rows:
        row.status = 'rejected'
        row.decided_at = stamp
        row.decided_by_id = user_id
    db.commit()
    return {'ids': [r.id for r in rows], 'decided_at': stamp.isoformat()}


def undo(db, ids, stamp, user_id):
    rows = db.query(PortalListing).filter(PortalListing.id.in_(ids),
        PortalListing.status == 'rejected', PortalListing.property_id.is_(None),
        PortalListing.decided_at == stamp, PortalListing.decided_by_id == user_id).with_for_update().all()
    for row in rows:
        row.status = 'pending'
        row.decided_at = None
        row.decided_by_id = None
    db.commit()
    return len(rows)
