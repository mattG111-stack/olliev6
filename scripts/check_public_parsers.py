"""Bounded read-only checks of previously observed public listing pages."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from portals.direct import Transport, CollectorUnavailable, USER_AGENT, canonical
from portals.page_data import extract

TARGETS = [
    ('oneroof', 'for_sale', 'https://www.oneroof.co.nz/search/houses-for-sale/region_auckland-35_page_1'),
    ('trademe', 'for_sale', 'https://www.trademe.co.nz/a/property/residential/sale/auckland/auckland-city/mount-wellington/listing/6132746188'),
    ('homes', 'for_sale', 'https://homes.co.nz/address/auckland/mount-wellington/638-mount-wellington-highway/gNlA0'),
]

def main():
    failed = False
    for source, kind, url in TARGETS:
        client = httpx.Client(timeout=30, follow_redirects=False, trust_env=False,
                             headers={'User-Agent': USER_AGENT, 'Accept': 'text/html'})
        transport = Transport(source, client=client)
        result = {'source': source, 'kind': kind, 'production_writes': False,
                  'proxy_verified': False}
        try:
            text = transport.get(url)
            records = extract(text, source)
            rows = [canonical(source, kind, u, raw) for u, raw in records.items()]
            usable = [r for r in rows if r.get('address') and r.get('suburb')]
            result.update(ok=bool(usable), records=len(rows), usable_identity=len(usable),
                          fields_present={field: sum(r.get(field) is not None for r in usable)
                            for field in ('price_numeric', 'beds', 'baths', 'floor_area_m2',
                                          'land_area_m2', 'building_age', 'building_age_decade',
                                          'sale_history', 'oneroof_estimate', 'homes_estimate')})
        except CollectorUnavailable as exc:
            result.update(ok=False, reason=str(exc))
        except Exception as exc:
            result.update(ok=False, error_type=type(exc).__name__)
        finally:
            transport.close()
        failed = failed or not result['ok']
        print(json.dumps(result), flush=True)
    return int(failed)

if __name__ == '__main__':
    raise SystemExit(main())
