"""Source-backed import checks. Unknown facts stay unknown."""
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

FACTS = {
    'beds': 'key_bedrooms', 'baths': 'key_bathrooms', 'cars': 'key_carspaces',
    'floor_area_m2': 'key_floor_area', 'land_area_m2': 'key_land_area',
    'cv_numeric': 'cv_numeric', 'zoning': 'zoning', 'building_age': 'building_age',
    'type_of_title': 'type_of_title', 'latitude': 'latitude', 'longitude': 'longitude',
    'image_url': 'image_1_url',
}

def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None

def nz_coordinates(lat, lng):
    """Reject missing halves and impossible NZ pins; not a parcel match."""
    lat, lng = number(lat), number(lng)
    return bool(lat is not None and lng is not None and -48 <= lat <= -33 and 165 <= lng <= 180)

def multiple_properties(address):
    text = str(address or '')
    return bool(re.search(r'\blots?\s*\d+\s*[-–,&]\s*\d+|\d+[a-z]?\s*&\s*[a-z\d]|^\d+[a-z]?\s*[-–,]\s*\d+', text, re.I))

def clean(value):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value if isinstance(value, (str, int, float, bool)) else str(value)

def snapshot(row, filename):
    fields = set(FACTS.values()) | {'address','suburb','district','region','url','slug_id',
        'price_display','price_numeric','sale_method','property_valuation_numeric',
        'property_valuation_low_numeric','property_valuation_high_numeric',
        'last_updated','listing_date','image_count','property_valuation_date'}
    return json.dumps({'filename': filename, 'captured_at': datetime.now(timezone.utc).isoformat(),
        'source_date': clean(row.get('last_updated')), 'url': clean(row.get('url')),
        'original': {key: clean(row.get(key)) for key in sorted(fields)}}, allow_nan=False)

def save_snapshots(db, pending, batch_id):
    """One original observation per accepted uploaded row; batched flush."""
    if not pending:
        return
    from models import PortalFinding
    db.flush()
    originals = {}
    ids = [getattr(prop, '_quality_origin_id', None) for prop, _ in pending]
    ids = [pid for pid in ids if pid]
    if ids:
        for finding in db.query(PortalFinding).filter(PortalFinding.property_id.in_(ids)):
            originals.setdefault(finding.property_id, []).append(finding)
    for prop, raw in pending:
        if not raw:
            for f in originals.get(getattr(prop, '_quality_origin_id', None), []):
                db.add(PortalFinding(property_id=prop.id, batch_id=batch_id, source=f.source,
                    field=f.field, kind=f.kind, status=f.status, value_num=f.value_num,
                    value_text=f.value_text, current_num=f.current_num, current_text=f.current_text,
                    extra_json=f.extra_json, created_at=f.created_at, decided_at=f.decided_at,
                    decided_by_id=f.decided_by_id))
            continue
        evidence = json.loads(raw)
        host = urlsplit(str(evidence.get('url') or '')).hostname or ''
        source = 'hougarden' if host.endswith('.hougarden.com') or host == 'hougarden.com' else 'upload'
        db.add(PortalFinding(property_id=prop.id, batch_id=batch_id, source=source,
            field='import_record', kind='fact', status='approved', extra_json=raw,
            decided_at=datetime.now(timezone.utc)))
    pending.clear()

def record_core_fact(db, prop, field, value, pv, previous=None):
    from models import PortalFinding
    db.add(PortalFinding(property_id=prop.id, batch_id=prop.import_batch_id,
        source='corelogic', field=field, kind='fact', status='approved',
        value_num=number(value), value_text=None if number(value) is not None else str(value)[:255],
        current_num=number(previous),
        extra_json=json.dumps({'url': pv.get('url'), 'previous_value': clean(previous),
            'address': prop.address, 'suburb': prop.suburb, 'observed_value': clean(value)}, allow_nan=False),
        decided_at=datetime.now(timezone.utc)))

def core_record(p):
    try:
        raw = json.loads(getattr(p,'pv_data',None) or '{}')
        return raw if isinstance(raw,dict) else {}
    except (ValueError,TypeError):
        return {}

def identity_mismatch(p):
    from addresses import address_key
    raw=core_record(p)
    if not raw.get('canonical_address') or not raw.get('suburb'):
        return False
    ours=address_key(getattr(p,'address',None),getattr(p,'suburb',None))
    theirs=address_key(raw['canonical_address'],raw['suburb'])
    return bool(ours and theirs and ours != theirs)

def source_disagreements(p):
    from propertyvalue import cross_check
    raw=core_record(p)
    if not raw or identity_mismatch(p):
        return []
    ours={field:number(getattr(p,attr,None)) for field,attr in
        [('floor_area_m2','floor_area_m2'),('land_area_m2','land_area_m2'),('cv','cv_numeric')]}
    theirs={field:number(raw.get(field)) for field in ours}
    ours['zoning']=getattr(p,'zoning',None);theirs['zoning']=raw.get('zoning')
    return cross_check(ours,theirs)

def checks(p):
    """All issues, including those hidden by a first-failure reject rule."""
    missing = []
    section = str(getattr(p, 'property_type', '') or '').casefold() in ('section','land','vacant land','lifestyle section')
    for field, label in [('cv_numeric','CV'), ('land_area_m2','land area')]:
        if number(getattr(p, field, None)) is None or number(getattr(p, field, None)) <= 0:
            missing.append(label)
    if not section and (number(getattr(p, 'floor_area_m2', None)) is None or number(getattr(p, 'floor_area_m2', None)) <= 0):
        missing.append('floor area')
    if not section:
        for field, label in [('beds','bedrooms'),('baths','bathrooms')]:
            if getattr(p, field, None) is None:
                missing.append(label)
    issues = ['Missing ' + x for x in missing]
    if not getattr(p,'address',None) or not getattr(p,'suburb',None):
        issues.append('Missing address/suburb — exact property match unavailable')
    for field,label in [('building_age','building age'),('type_of_title','title'),('zoning','zoning')]:
        if not getattr(p,field,None):
            issues.append('Missing '+label)
    if multiple_properties(getattr(p, 'address', None)):
        issues.append('Multiple properties/lots — verify exact title and areas')
    if identity_mismatch(p):
        issues.append('CoreLogic record belongs to a different exact address/unit')
    issues += ['Source disagreement: '+item['field'] for item in source_disagreements(p)]
    lat, lng = getattr(p, 'latitude', None), getattr(p, 'longitude', None)
    if lat is not None or lng is not None:
        if not nz_coordinates(lat, lng):
            issues.append('Invalid NZ coordinates — map pin excluded')
    else:
        issues.append('Map location unavailable')
    if not getattr(p, 'image_url', None):
        issues.append('Cover photo unavailable')
    ask, estimate = number(getattr(p,'asking_price',None)), number(getattr(p,'third_party_valuation',None))
    if ask and estimate and ask == estimate:
        issues.append('Feed price equals portal estimate — verify price provenance')
    return issues

def subdivision_checks(p):
    entries = [f"Zoning: {getattr(p,'zoning',None) or 'missing'}",
        f"Title: {getattr(p,'type_of_title',None) or 'missing'}",
        f"Listing land-area check: {getattr(p,'land_area_flag',None) or 'not recorded'}"]
    # A calculated services cost is a budget, never evidence of service capacity.
    entries += ['Overlays: not verified', 'Legal access: not verified',
        'Service capacity: not verified', 'Council consent: not verified']
    return ' · '.join(entries)

def provenance(p, findings=()):
    lines = []
    for f in findings:
        if f.field == 'import_record':
            try:
                raw = json.loads(f.extra_json or '{}')
                lines.append(f"{f.source}: {raw.get('filename')} · captured {raw.get('captured_at')} · source date {raw.get('source_date') or 'not supplied'}")
            except (TypeError, ValueError):
                pass
        elif f.status == 'approved':
            lines.append(f"{f.field}: {f.source} · {f.decided_at or f.created_at}")
        elif f.kind == 'conflict':
            lines.append(f"Conflict: {f.field} · {f.source}")
    if getattr(p, 'pv_checked_at', None):
        lines.append(f"CoreLogic checked: {p.pv_checked_at}" + (' · record retained' if getattr(p,'pv_data',None) else ' · no record retained'))
    for item in source_disagreements(p):
        lines.append(f"{item['field']}: stored {item['ours']} / CoreLogic {item['theirs']} · source date {getattr(p,'pv_checked_at',None) or 'not retained'}")
    return ' | '.join(lines) or 'Original field provenance not retained on this older record'
