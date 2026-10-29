"""Retained, approved asking-price observations. Never infer a seller change date."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import math
import re
from zoneinfo import ZoneInfo

from addresses import address_key
from models import AskingPriceObservation as O, ImportBatch, PropertyForSale as P
from prior_price import _was_really_advertised


def identity(p):
    # Require an exact numbered street and suburb. Units remain part of the key;
    # never merge by portal ID alone (feeds have reused IDs for different units).
    if not p.suburb or not p.address or not re.search(r'\d', p.address.split(',')[0]):
        return None
    if re.search(r'\s(?:&|and)\s|\d\s*[-–]\s*\d', p.address.split(',')[0], re.I):
        return None
    key = address_key(p.address, p.suburb)
    return hashlib.sha256(f'{(p.region or "Auckland").strip().casefold()}|{key}'.encode()).hexdigest()


def advertised(p):
    try:
        valid = math.isfinite(float(p.asking_price)) and p.asking_price >= 10000
    except (ValueError, TypeError):
        return False
    # Search prices, floors, auction guides and derived working prices cannot
    # become an advertised-price reduction, even if they are positive numbers.
    method = (p.sale_method or '').casefold()
    return bool(valid and p.url and _was_really_advertised(p)
                and not re.search(r'auction|negotiat|tender|deadline|enquir|offers? over', method))


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def record_batch(db, batch_id):
    """Append a single approved batch, in its publication transaction. Idempotent."""
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.batch_type != 'for_sale' or batch.status not in ('published', 'archived'):
        return 0
    when = batch.created_at
    if not when or _utc(when) > datetime.now(timezone.utc):
        return 0
    if db.get_bind().dialect.name == 'postgresql':
        from sqlalchemy import text
        db.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
                   {'key': 'apex:asking-price-history'})
    existing = {key for key, in db.query(O.identity_key).filter(
        O.batch_id == batch_id, O.observed_at == when)}
    groups = defaultdict(list)
    for p in db.query(P).filter(P.import_batch_id == batch_id, P.is_held.is_(False)):
        key = identity(p)
        if key:
            groups[key].append(p)
    count = 0
    for key, rows in groups.items():
        # Ambiguous duplicate rows are not an exact property observation.
        if key in existing or len(rows) != 1 or not advertised(rows[0]):
            continue
        p = rows[0]
        db.add(O(event_key=hashlib.sha256(f'{batch_id}|{_utc(when).isoformat()}|{key}'.encode()).hexdigest(),
                 identity_key=key, region=p.region or 'Auckland', batch_id=batch_id,
                 property_id=p.id, address=p.address, suburb=p.suburb,
                 source_url=p.url, price=p.asking_price, observed_at=when))
        count += 1
    db.flush()
    return count


def bootstrap(db):
    """Retain existing approved uploads using their actual received dates."""
    total = 0
    for bid, when in db.query(ImportBatch.id, ImportBatch.created_at).filter(ImportBatch.batch_type == 'for_sale',
            ImportBatch.status.in_(('published', 'archived'))).order_by(ImportBatch.id):
        # A baseline is committed atomically. Subsequent releases/individual
        # approvals capture their own observations; do not rescan old books on
        # every API restart.
        if db.query(O.id).filter(O.batch_id == bid, O.observed_at == when).first():
            continue
        total += record_batch(db, bid)
    return total


def histories(db, properties):
    keys = {identity(p) for p in properties} - {None}
    result = defaultdict(list)
    if keys:
        for o in db.query(O).filter(O.identity_key.in_(keys)).order_by(O.observed_at, O.batch_id, O.id):
            if _utc(o.observed_at) <= datetime.now(timezone.utc):
                result[o.identity_key].append(o)
    return result


def points(observations):
    return [{'recorded_at': _utc(o.observed_at).isoformat(),
             'recorded_date': _utc(o.observed_at).astimezone(ZoneInfo('Pacific/Auckland')).date().isoformat(),
             'asking_price': o.price, 'source_url': o.source_url} for o in observations]


def summary(p, observations):
    # Old observations remain visible in the timeline, but never claim a current
    # reduction against a withdrawn/derived price or an unrecorded private edit.
    if not advertised(p) or len(observations) < 2:
        return None
    last = observations[-1]
    if last.batch_id != p.import_batch_id or last.price != p.asking_price:
        return None
    # Repeated unchanged uploads must not erase the last actual price movement.
    i = len(observations) - 1
    while i > 0 and observations[i-1].price == last.price:
        i -= 1
    if i == 0:
        return None
    before, after = observations[i-1], observations[i]
    pct = (after.price / before.price - 1) * 100
    # Retain outliers as evidence, but do not advertise them as seller reductions.
    if abs(pct) > 50:
        return None
    return {'previous_price': before.price, 'current_price': last.price,
            'change_dollars': last.price - before.price, 'change_pct': round(pct, 2),
            'previous_recorded_at': _utc(before.observed_at).isoformat(),
            'recorded_at': _utc(after.observed_at).isoformat(),
            'source_url': after.source_url}


def attach(db, properties):
    all_history = histories(db, properties)
    for p in properties:
        p.asking_price_change = summary(p, all_history.get(identity(p), []))


def evidence(db, p):
    history = histories(db, [p]).get(identity(p), [])
    return {'observations': points(history), 'latest_change': summary(p, history),
            'date_basis': 'Date Apex received an approved advertised price; exact seller change date unknown.'}
