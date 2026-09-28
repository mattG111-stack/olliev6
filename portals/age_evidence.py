"""Recognize compatible year/decade evidence without inventing an exact year."""
import json
import re
from datetime import datetime, timezone


def compatible(values):
    bounds = []
    for value in values:
        match = re.fullmatch(r'(\d{4})(s)?', str(value).strip())
        if not match:
            return False
        year = int(match[1])
        if not 1800 <= year <= 2100 or (match[2] and year % 10):
            return False
        bounds.append((year, year + (9 if match[2] else 0)))
    return len(bounds) >= 2 and max(x[0] for x in bounds) <= min(x[1] for x in bounds)


def resolve_compatible_age(row):
    """Archive only proven compatible age flags; leave every other hold intact."""
    try:
        raw = json.loads(row.raw_json or '{}')
    except (ValueError, TypeError):
        return False
    if not isinstance(raw, dict):
        return False
    changed = False
    for key in ('source_conflicts', 'conflicts'):
        conflicts = raw.get(key)
        if not isinstance(conflicts, dict):
            continue
        evidence = conflicts.get('building_age')
        if not isinstance(evidence, list) or not evidence:
            continue
        values = [v.get('value') if isinstance(v, dict) else v for v in evidence]
        values += [v for v in (row.building_age, raw.get('building_age')) if v]
        if not compatible(values):
            continue
        raw.setdefault('compatible_age_resolutions', []).append({
            'field': 'building_age', 'container': key, 'evidence': evidence,
            'values_checked': values, 'reason': 'Exact year lies within reported decade',
            'resolved_at': datetime.now(timezone.utc).isoformat()})
        del conflicts['building_age']
        changed = True
    if changed:
        generic = 'Source records disagree — review source evidence before approval'
        if not raw.get('conflicts') and not raw.get('source_conflicts') and row.price_flag == generic:
            raw['resolved_age_price_flag'] = row.price_flag
            row.price_flag = None
        row.raw_json = json.dumps(raw, ensure_ascii=False)
    return changed
