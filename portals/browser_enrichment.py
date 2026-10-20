"""Save bounded, visible-browser observations; never fetch source pages here."""
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case
from sqlalchemy.orm import Session

from addresses import address_key
from data_quality import multiple_properties
from db import get_db
from models import ImportBatch, IngestJob, PortalFinding, PropertyForSale, User
from security import require_admin

router = APIRouter()
FIELDS = {'floor_area_m2': (10, 2000), 'land_area_m2': (10, 500000),
          'cv_numeric': (50000, 100000000), 'beds': (0, 20),
          'baths': (0, 20), 'cars': (0, 20)}
LABELS = {'floor_area_m2': r'\bfloor\b', 'land_area_m2': r'\bland\b',
          'cv_numeric': r'\b(cv|capital value|rateable value|rating valuation)\b',
          'beds': r'\b(beds?|bedrooms?)\b', 'baths': r'\b(baths?|bathrooms?)\b',
          'cars': r'\b(cars?|carparks?|parking|garages?)\b'}


class Fact(BaseModel):
    model_config = ConfigDict(extra='forbid')
    value: float
    quote: str = Field(min_length=1, max_length=500)


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    property_id: int = Field(gt=0)
    address: str = Field(min_length=1, max_length=255)
    suburb: str = Field(min_length=1, max_length=128)
    source_url: str = Field(max_length=1024)
    observed_at: datetime
    evidence: str = Field(min_length=10, max_length=16000)
    facts: dict[str, Fact] = Field(default_factory=dict, max_length=6)
    property_type: str | None = Field(default=None, max_length=128)
    sale_method: str | None = Field(default=None, max_length=128)

    @field_validator('source_url')
    @classmethod
    def source_link(cls, value):
        u = urlsplit(value)
        if (u.scheme != 'https' or u.hostname not in ('trademe.co.nz', 'www.trademe.co.nz')
                or u.username or u.password or u.port not in (None, 443)
                or not re.fullmatch(r'/a/property/(?:residential|new-homes)/.+/listing/\d+/?', u.path)):
            raise ValueError('Use the observed public Trade Me property listing link')
        return urlunsplit(('https', 'www.trademe.co.nz', u.path.rstrip('/'), '', ''))


class SaveObservations(BaseModel):
    model_config = ConfigDict(extra='forbid')
    batch_id: int = Field(gt=0)
    observations: list[Observation] = Field(min_length=1, max_length=10)


def scope(db, batch_id, *, lock=False):
    query = db.query(ImportBatch).filter_by(id=batch_id)
    batch = (query.with_for_update() if lock else query).one_or_none()
    if (batch is None or batch.batch_type != 'for_sale' or batch.is_active
            or batch.status not in ('staged', 'preview')):
        raise HTTPException(409, 'Select a staged for-sale batch; live data cannot be changed here')
    core = db.query(IngestJob).filter(
        IngestJob.batch_id == batch_id,
        IngestJob.filename.like('enrich (batch %')).order_by(IngestJob.id.desc()).first()
    try:
        ids = json.loads(core.result_json or '{}')['ids'] if core else None
        if not isinstance(ids, list) or not all(type(i) is int and i > 0 for i in ids):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, 'No saved CoreLogic listing scope; no wider batch was selected')
    return batch, core, set(ids)


@router.get('/release/browser-enrichment')
def queue(batch_id: int, limit: int = Query(10, ge=1, le=25),
          _: User = Depends(require_admin), db: Session = Depends(get_db)):
    batch, core, ids = scope(db, batch_id)
    markers = db.query(PortalFinding).filter_by(
        batch_id=batch.id, source='trademe_browser', field='browser_observation').all()
    done = {f.property_id for f in markers}
    reviews = []
    for f in markers:
        if f.status == 'review':
            raw = json.loads(f.extra_json or '{}')
            reviews.append({'property_id': f.property_id, 'address': raw.get('address'),
                            'issues': f.value_text, 'source_url': raw.get('source_url')})
    rows = db.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == batch.id, PropertyForSale.id.in_(ids - done)
    ).order_by(case(
        (PropertyForSale.floor_area_m2.is_(None), 0),
        (PropertyForSale.cv_numeric.is_(None), 1),
        (PropertyForSale.land_area_m2.is_(None), 2), else_=3),
        PropertyForSale.id).limit(limit).all()
    return {'batch_id': batch.id, 'corelogic_job_id': core.id, 'scope_total': len(ids),
            'observed': len(ids & done), 'remaining': len(ids - done),
            'source_review_count': len(reviews), 'source_reviews': reviews[:25],
            'listings': [{'id': p.id, 'address': p.address, 'suburb': p.suburb,
                          'source_url': p.url, 'property_type': p.property_type,
                          'missing': [k for k in FIELDS if getattr(p, k) is None]}
                         for p in rows]}


def validate_observation(p, item):
    now = datetime.now(timezone.utc)
    if item.observed_at.tzinfo is None or not -300 <= (now - item.observed_at).total_seconds() <= 86400:
        raise HTTPException(422, 'Observation needs a timezone and must be from the last 24 hours')
    if (multiple_properties(item.address) or multiple_properties(p.address)
            or address_key(p.address, p.suburb) != address_key(item.address, item.suburb)):
        raise HTTPException(422, 'Exact street, unit and suburb must match')
    normalized = re.sub(r'\s+', ' ', item.evidence).casefold()
    if (item.address.casefold() not in normalized or item.suburb.casefold() not in normalized):
        raise HTTPException(422, 'Visible source evidence must include the address and suburb')
    for name, fact in item.facts.items():
        if name not in FIELDS or not math.isfinite(fact.value):
            raise HTTPException(422, 'Unsupported or invalid fact')
        low, high = FIELDS[name]
        if not low <= fact.value <= high or (name in ('beds', 'baths', 'cars') and fact.value % 1):
            raise HTTPException(422, 'Fact is outside the plausible range')
        if fact.quote not in item.evidence:
            raise HTTPException(422, 'Every fact must quote the visible source evidence')
        if not re.search(LABELS[name], fact.quote, re.I):
            raise HTTPException(422, 'Source quote must name the supplied fact')
        numbers = re.findall(r'\d[\d,]*(?:\.\d+)?', fact.quote)
        if not any(abs(float(n.replace(',', '')) - fact.value) < 0.0001 for n in numbers):
            raise HTTPException(422, 'Fact value does not match its source quote')
    for value in (item.property_type, item.sale_method):
        if value and value.casefold() not in normalized:
            raise HTTPException(422, 'Property type and sale method must appear in the source evidence')


def apply_observation(db, p, item, admin_id, reprice):
    validate_observation(p, item)
    old = db.query(PortalFinding).filter_by(property_id=p.id, batch_id=p.import_batch_id,
        source='trademe_browser', field='browser_observation').first()
    if old:
        return {'property_id': p.id, 'status': 'already_saved', 'filled': 0, 'conflicts': []}
    conflicts = []
    if item.property_type and p.property_type and item.property_type.casefold() != p.property_type.casefold():
        conflicts.append('property_type')
    # Existing asking prices are never overwritten or inferred from these observations.
    if item.sale_method and p.sale_method and item.sale_method.casefold() != p.sale_method.casefold():
        conflicts.append('sale_method')
    for name, fact in item.facts.items():
        current = getattr(p, name)
        if current is not None and abs(float(current) - fact.value) > 0.001:
            conflicts.append(name)
    payload = item.model_dump(mode='json')
    payload['previous'] = {k: getattr(p, k) for k in item.facts}
    payload['previous'].update(property_type=p.property_type, sale_method=p.sale_method,
        asking_price=p.asking_price, is_held=p.is_held, hold_reason=p.hold_reason)
    marker = PortalFinding(property_id=p.id, batch_id=p.import_batch_id,
        source='trademe_browser', field='browser_observation', kind='fact',
        status='review' if conflicts else 'approved',
        extra_json=json.dumps(payload), decided_by_id=admin_id,
        decided_at=datetime.now(timezone.utc) if not conflicts else None)
    db.add(marker)
    filled = 0
    if not conflicts:
        for name, fact in item.facts.items():
            if getattr(p, name) is not None:
                continue
            value = int(fact.value) if name in ('beds', 'baths', 'cars') else fact.value
            setattr(p, name, value)
            db.add(PortalFinding(property_id=p.id, batch_id=p.import_batch_id,
                source='trademe_browser', field=name, kind='fact', status='approved',
                value_num=value, extra_json=json.dumps({'url': item.source_url,
                    'observed_at': item.observed_at.isoformat(), 'quote': fact.quote}),
                decided_by_id=admin_id, decided_at=datetime.now(timezone.utc)))
            filled += 1
        # Same pricing/hold checks as the existing per-listing Re-price action.
        # A failure aborts this transaction; no filled facts get stale valuations.
        if filled:
            db.flush()
            reprice(db, p.id)
    if conflicts:
        # Do not apply conflicting claims or publish this row automatically.
        # Keep the evidence visible for review; preserve the original values.
        marker.value_text = ', '.join(conflicts)
        p.is_held = True
        p.hold_reason = 'Source review: Trade Me differs on ' + ', '.join(conflicts)
    db.commit()
    return {'property_id': p.id, 'status': 'review' if conflicts else 'saved',
            'filled': filled, 'conflicts': conflicts}


@router.post('/release/browser-enrichment')
def save(body: SaveObservations, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if len({o.property_id for o in body.observations}) != len(body.observations):
        raise HTTPException(422, 'Duplicate listing IDs in the request')
    results = []
    from reprice import reprice_one
    # Per-row durable commits preserve earlier successes if the connection stops.
    for item in body.observations:
        batch, core, ids = scope(db, body.batch_id, lock=True)
        if item.property_id not in ids:
            db.rollback()
            raise HTTPException(422, 'Listing is outside the saved CoreLogic scope')
        p = db.query(PropertyForSale).filter_by(id=item.property_id, import_batch_id=batch.id
            ).populate_existing().with_for_update().one_or_none()
        if p is None:
            raise HTTPException(404, 'Listing no longer exists in this batch')
        try:
            results.append(apply_observation(db, p, item, admin.id,
                lambda session, pid: reprice_one(session, pid, region=batch.region)))
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise HTTPException(503, 'Could not save and price this observation; this row was rolled back')
    return {'batch_id': body.batch_id, 'filled': sum(r['filled'] for r in results), 'results': results}
