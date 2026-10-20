"""Compare saved, dated HouGarden opinions with Apex; never recalibrate values."""
from datetime import date, datetime, timezone
from math import isfinite
from statistics import median
from urllib.parse import urlparse
import re
import json
from models import PropertyForSale, PortalFinding
from data_quality import identity_mismatch, multiple_properties


def benchmark(db, batch, limit=50):
    excluded = {'missing_estimate': 0, 'unverified_source': 0, 'undated_or_future': 0,
                'identity_review': 0, 'missing_apex': 0}
    from addresses import address_key
    evidence = {}
    for finding in db.query(PortalFinding).filter_by(batch_id=batch.id, source='hougarden',
            field='import_record', status='approved').order_by(PortalFinding.id):
        try:
            evidence[finding.property_id] = json.loads(finding.extra_json or '{}')
        except (ValueError,TypeError):
            continue
    rows = []
    total = 0
    today = datetime.now(timezone.utc).date()
    for p in db.query(PropertyForSale).filter_by(import_batch_id=batch.id):
        total += 1
        hg = p.third_party_valuation
        if hg is None or not isfinite(hg) or hg <= 0:
            excluded['missing_estimate'] += 1; continue
        host = (urlparse(p.url or '').hostname or '').lower()
        if not (host == 'hougarden.com' or host.endswith('.hougarden.com')):
            excluded['unverified_source'] += 1; continue
        raw = evidence.get(p.id, {})
        original = raw.get('original', {})
        try:
            same_value = abs(float(original.get('property_valuation_numeric')) - hg) < 0.01
        except (ValueError,TypeError):
            same_value = False
        if not same_value or address_key(original.get('address'),original.get('suburb')) != address_key(p.address,p.suburb):
            excluded['unverified_source'] += 1; continue
        # cv_valuation_date maps to valuation_last_date in legacy imports. It is
        # NOT the HouGarden estimate date and must never be labelled as one.
        try:
            as_of = date.fromisoformat(str(raw.get('captured_at') or '')[:10])
            if as_of > today: raise ValueError()
        except (ValueError, TypeError):
            excluded['undated_or_future'] += 1; continue
        if not p.address or not re.search(r'\d', p.address) or multiple_properties(p.address) or identity_mismatch(p) or (p.hold_reason or '').startswith('Source review:'):
            excluded['identity_review'] += 1; continue
        apex = p.fair_value
        if apex is None or not isfinite(apex) or apex <= 0:
            excluded['missing_apex'] += 1; continue
        rows.append({'id': p.id, 'address': p.address, 'suburb': p.suburb,
            'apex': apex, 'hougarden': hg, 'observed_date': as_of.isoformat(),
            'source_url': p.url, 'difference': apex - hg,
            'difference_pct': (apex / hg - 1) * 100, 'held': bool(p.is_held)})
    # One vote per exact identity; repeated campaigns must not skew the benchmark.
    from addresses import address_key
    unique = {}
    for row in rows:
        key = address_key(row['address'], row['suburb'])
        prior = unique.get(key)
        if prior is None or (row['observed_date'],row['id']) > (prior['observed_date'],prior['id']):
            unique[key] = row
    deduplicated = len(rows) - len(unique)
    rows = list(unique.values())
    gaps = [r['difference_pct'] for r in rows]
    return {'batch_id': batch.id, 'batch_status': batch.status, 'total_rows': total,
        'matched_properties': len(rows), 'duplicate_observations_excluded': deduplicated,
        'median_difference_pct': median(gaps) if gaps else None,
        'median_absolute_difference_pct': median([abs(g) for g in gaps]) if gaps else None,
        'over_20_pct': sum(abs(g) > 20 for g in gaps), 'excluded': excluded,
        'oldest_observation': min((r['observed_date'] for r in rows), default=None),
        'newest_observation': max((r['observed_date'] for r in rows), default=None),
        'note': 'Saved HouGarden estimates compared with current Apex values. Dates show when Apex received the source record; the estimate effective date is unknown. This measures agreement, not accuracy against sale prices. Held rows are labelled; no values are changed.',
        'rows': sorted(rows,key=lambda r: -abs(r['difference_pct']))[:limit]}
