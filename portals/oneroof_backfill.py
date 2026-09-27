"""Exact-property OneRoof backfill using observed links, never guessed URLs."""
import json
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from addresses import address_key
from models import PortalListing, AppSetting
from portals import direct, page_data
from portals.listings import to_listing

FIELDS = ('cv_numeric', 'land_value_numeric', 'improvement_value_numeric',
          'floor_area_m2', 'land_area_m2', 'beds', 'baths', 'carspaces',
          'building_age', 'type_of_title', 'zoning', 'condition',
          'image_url', 'image_urls', 'image_count')


def evidence(row):
    try:
        value = json.loads(row.raw_json or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def property_url(url):
    try:
        direct.validate_url(url, 'oneroof')
        return url if urlsplit(url).path.startswith('/property/') else None
    except (ValueError, TypeError, direct.CollectorUnavailable):
        return None


def known_url(db, row):
    raw = evidence(row)
    for url in [row.url] + list(raw.get('source_urls') or []):
        if property_url(url):
            return url
    key = address_key(row.address, row.suburb)
    if not key or not row.suburb:
        return None
    matches = db.query(PortalListing).filter(
        PortalListing.source == 'oneroof', PortalListing.kind == 'for_sale',
        PortalListing.address_key == key).order_by(PortalListing.id.desc()).all()
    urls = {property_url(r.url) for r in matches if
            address_key(r.address, r.suburb) == key and
            (not row.district or not r.district or row.district.casefold() == r.district.casefold())}
    urls.discard(None)
    # Different listing URLs require review rather than choosing arbitrarily.
    return next(iter(urls)) if len(urls) == 1 else None


def fetch(url):
    transport = direct.Transport('oneroof')
    try:
        html = transport.get(url)
        final = transport.resolved_urls.get(url, url)
        direct.validate_url(final, 'oneroof')
        items = page_data.extract(html, 'oneroof')
        if final not in items:
            return None
        return direct.canonical('oneroof', 'for_sale', final, items[final])
    finally:
        transport.close()


def apply(row, item):
    """Fill blanks, retain disagreements and raw evidence, never approve or reprice."""
    key = address_key(row.address, row.suburb)
    # Reject ranges/multiple properties rather than collapsing punctuation.
    if (not key or not row.suburb or not item.get('suburb') or
        any(c in str(row.address).split(',')[0] for c in ('&', '-')) or
        any(c in str(item.get('address', '')).split(',')[0] for c in ('&', '-')) or
        key != address_key(item.get('address'), item.get('suburb')) or
        (row.district and item.get('district') and
         row.district.strip().casefold() != item['district'].strip().casefold())):
        return 0, 'identity_mismatch'
    fresh = to_listing('oneroof', item)
    if not fresh:
        return 0, 'not_found'
    from portals.complete import _blank
    raw = evidence(row)
    conflicts = dict(raw.get('source_conflicts') or {})
    new_conflicts = item.get('source_conflicts') or {}
    fields = []
    for field in FIELDS:
        value = fresh.get(field)
        if field in new_conflicts or _blank(value):
            continue
        if isinstance(value, (float, int)) and (not math.isfinite(value) or value <= 0):
            continue
        current = getattr(row, field)
        if _blank(current):
            setattr(row, field, value)
            fields.append(field)
        elif field not in ('image_url', 'image_urls', 'image_count') and str(current) != str(value):
            # Numeric int/float representations are equivalent.
            try:
                equal = float(current) == float(value)
            except (ValueError, TypeError):
                equal = str(current).strip().casefold() == str(value).strip().casefold()
            if not equal:
                conflicts[field] = [current, value]
    conflicts.update(new_conflicts)
    raw['source_conflicts'] = conflicts
    raw['oneroof_backfill'] = {'url': item['url'], 'collected_at': item['scraped_at'],
                              'filled_fields': fields, 'evidence': item}
    # Keep original source identity, timestamp and evidence; do not make stale
    # asking-price evidence look fresh merely because measurements were filled.
    provenance = raw.setdefault('backfill_provenance', {})
    for field in fields:
        provenance[field] = {'source': 'oneroof', 'url': item['url'],
                             'collected_at': item['scraped_at']}
    row.raw_json = json.dumps(raw, ensure_ascii=False)
    return len(fields), 'ok'


def fill(db, row):
    if row.kind != 'for_sale':
        return None
    url = known_url(db, row)
    if not url:
        return None
    from portals import checkpoints
    now = datetime.now(timezone.utc)
    cooldown = db.get(AppSetting, 'scraper.backfill.oneroof.retry_after')
    for until in (cooldown.value if cooldown else None,
                  checkpoints.load(db, 'oneroof', 'for_sale').get('source_retry_after')):
        try:
            if until and datetime.fromisoformat(until) > now:
                return 0, 'unreachable'
        except (ValueError, TypeError):
            pass
    try:
        item = fetch(url)
        return apply(row, item) if item else (0, 'not_found')
    except direct.CollectorUnavailable:
        # Share a durable pause across separate fill calls/processes. Never
        # rotate to another proxy after a restriction or retry every next row.
        if cooldown is None:
            cooldown = AppSetting(key='scraper.backfill.oneroof.retry_after')
            db.add(cooldown)
        cooldown.value = (now + timedelta(hours=1)).isoformat()
        return 0, 'unreachable' 
