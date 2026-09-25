"""Bounded public-page collector. No Apify or API credentials from listing sites.

Proxy gateway credentials stay server-side. Rotation is between collection jobs,
not a way to retry a denied request. Source failures fail the job rather than
being mistaken for zero listings or evidence that a property was delisted.
"""
from __future__ import annotations
import itertools
import json
import threading
import time
import re
from urllib.parse import urlsplit, urljoin, parse_qs
from urllib.robotparser import RobotFileParser

import httpx
from config import settings
from portals import page_data

HOSTS = {
    'oneroof': {'www.oneroof.co.nz', 'oneroof.co.nz'},
    'realestate': {'www.realestate.co.nz', 'realestate.co.nz'},
    'trademe': {'www.trademe.co.nz', 'trademe.co.nz'},
    'homes': {'www.homes.co.nz', 'homes.co.nz'},
}
USER_AGENT = 'ApexPropertyCollector/2.0'
MAX_BYTES = 5_000_000
_lock = threading.Lock()
_rotation = itertools.count()
_last_request = {}

class CollectorUnavailable(RuntimeError):
    pass


def configured(db=None):
    if not settings.scraper_enabled:
        return False
    try:
        proxies = json.loads(settings.scraper_proxy_urls)
        seeds = json.loads(settings.scraper_seeds)
        return bool(isinstance(proxies, list) and proxies and
                    all(isinstance(p, str) and urlsplit(p).scheme in ('http', 'https') and urlsplit(p).hostname for p in proxies) and
                    isinstance(seeds, dict) and seeds)
    except (ValueError, TypeError):
        return False


def status():
    # Do not expose any URL, username, password or exception containing them.
    return {'configured': configured(), 'proxy_configured': bool(settings.scraper_proxy_urls),
            'enabled': settings.scraper_enabled, 'review_required': True,
            'message': 'Ready for a supervised collection test' if configured() else
                       'Collection disabled or awaiting proxy and source configuration'}


def validate_url(url, source):
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname not in HOSTS.get(source, set()) or u.username or u.password or u.port not in (None, 443):
        raise CollectorUnavailable('Unsupported source URL')
    return url


class Transport:
    def __init__(self, source, *, client=None, delay=3.0):
        if source not in HOSTS:
            raise CollectorUnavailable('Unknown source')
        self.source, self.delay, self.robots = source, max(1.0, delay), {}
        self.blocked = False
        self.proxy_url = None
        if client is not None:
            self.client = client
            return
        if not configured():
            raise CollectorUnavailable('Configure the scraper before collecting')
        try:
            urls = json.loads(settings.scraper_proxy_urls)
            if not isinstance(urls, list) or not urls or not all(isinstance(u, str) and urlsplit(u).scheme in ('http', 'https') and urlsplit(u).hostname for u in urls):
                raise ValueError()
            with _lock:
                proxy = urls[next(_rotation) % len(urls)]
            self.proxy_url = proxy
            self.client = httpx.Client(proxy=proxy, timeout=30, follow_redirects=False,
                                       trust_env=False, headers={'User-Agent': USER_AGENT, 'Accept': 'text/html'})
        except Exception:
            raise CollectorUnavailable('Invalid proxy configuration') from None

    def close(self):
        self.client.close()

    def _read(self, url):
        validate_url(url, self.source)
        if self.blocked:
            raise CollectorUnavailable('Source paused after access restriction')
        # Shared per-origin pacing survives calls and proxy changes in this worker.
        host = urlsplit(url).hostname
        with _lock:
            pause = self.delay - (time.monotonic() - _last_request.get(host, 0))
            if pause > 0:
                time.sleep(pause)
            _last_request[host] = time.monotonic()
        try:
            with self.client.stream('GET', url) as response:
                if response.headers.get('x-amzn-waf-action') in ('challenge', 'captcha'):
                    self.blocked = True
                    raise CollectorUnavailable('Source paused: access challenge; no proxy retry')
                if response.status_code in (401, 403, 429):
                    self.blocked = True
                    raise CollectorUnavailable(f'Source paused: HTTP {response.status_code}; no proxy retry')
                if 300 <= response.status_code < 400:
                    # Redirects must go through the same URL and robots checks.
                    return response.status_code, response.headers.get('location', ''), ''
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise CollectorUnavailable('Page exceeds collection size limit')
                text = data.decode('utf-8', errors='replace')
                if any(marker in text.lower() for marker in ('cf-chl-', 'g-recaptcha', 'hcaptcha', 'verify you are human', 'access denied')):
                    self.blocked = True
                    raise CollectorUnavailable('Source paused: access challenge')
                return response.status_code, '', text
        except CollectorUnavailable:
            raise
        except Exception:
            raise CollectorUnavailable('Source or proxy connection failed') from None

    def get(self, url, redirects=0):
        validate_url(url, self.source)
        u = urlsplit(url)
        origin = f'{u.scheme}://{u.netloc}'
        if origin not in self.robots:
            code, _, text = self._read(origin + '/robots.txt')
            if code not in (200, 404):
                raise CollectorUnavailable('Could not establish robots rules')
            rules = RobotFileParser()
            rules.parse(text.splitlines() if code == 200 else [])
            self.robots[origin] = rules
        rules = self.robots[origin]
        if not rules.can_fetch(USER_AGENT, url):
            raise CollectorUnavailable('Source robots rules disallow this URL')
        self.delay = max(self.delay, float(rules.crawl_delay(USER_AGENT) or 0))
        code, location, text = self._read(url)
        if 300 <= code < 400:
            if redirects >= 3:
                raise CollectorUnavailable('Too many source redirects')
            return self.get(urljoin(url, location), redirects + 1)
        if code != 200:
            raise CollectorUnavailable(f'Source returned HTTP {code}')
        if self.source == 'homes' and u.path.startswith('/map'):
            if not settings.scraper_render_homes:
                raise CollectorUnavailable('Homes discovery requires the enabled browser renderer')
            from portals.rendered import render_homes
            return render_homes(url, proxy=self.proxy_url)
        return text


def canonical(source, kind, url, raw):
    n = page_data.normalise(source, kind, url, raw)
    aliases = {'listing_id': 'source_id', 'key_bedrooms': 'beds', 'key_bathrooms': 'baths',
               'key_carspaces': 'carspaces', 'key_floor_area': 'floor_area_m2',
               'key_land_area': 'land_area_m2', 'year_built': 'building_age',
               'key_time_on_market': 'days_on_market'}
    row = {aliases.get(k, k): v for k, v in n.items()}
    row.update(kind=kind, source=source, url=url, _apex_direct=True)
    row['raw_source'] = raw
    row['provenance'] = {aliases.get(k, k): {'source': source, 'url': url, 'collected_at': n['scraped_at']}
                         for k in n if k not in ('source', 'url', 'scraped_at')}
    return row


def next_pages(text, url, source):
    """Follow only links actually present, with unchanged search filters."""
    from html.parser import HTMLParser
    found=[]
    class Links(HTMLParser):
        def handle_starttag(self,tag,attrs):
            a=dict(attrs)
            if tag not in ('a','link') or not a.get('href'):return
            target=urljoin(url,a['href']);u=urlsplit(url);v=urlsplit(target)
            explicit='next' in a.get('rel','').split() or a.get('aria-label','').lower().startswith('next page')
            if source=='oneroof':
                old=re.search(r'_page_(\d+)$',u.path);new=re.search(r'_page_(\d+)$',v.path)
                sequential=bool(old and new and int(new[1])==int(old[1])+1 and u.path[:old.start()]==v.path[:new.start()] and u.query==v.query)
            else:
                q=parse_qs(u.query);r=parse_qs(v.query)
                sequential=u.path==v.path and all(r.get(k)==value for k,value in q.items() if k!='page') and set(r)-{'page'}==set(q)-{'page'}
                try:sequential=sequential and int(r.get('page',['1'])[0])==int(q.get('page',['1'])[0])+1
                except (ValueError,IndexError):sequential=False
            if (explicit or source=='oneroof') and sequential:
                validate_url(target,source)
                if target not in found:found.append(target)
    Links().feed(text)
    return found


def collect(source, *, kind='for_sale', cap=300, suburb=None, transport=None):
    """Walk configured search pages + visible next links within explicit bounds.

    Seeds are explicit per source/category. No guessed private API endpoints,
    login, CAPTCHA solving or false claim of complete city-wide coverage.
    """
    if kind not in ('for_sale', 'sold', 'rent') or source not in HOSTS:
        raise CollectorUnavailable('Unsupported collection scope')
    try:
        seeds = json.loads(settings.scraper_seeds).get(source, {}).get(kind, [])
        if not isinstance(seeds, list) or not seeds or len(seeds) > 10:
            raise ValueError()
        for url in seeds:
            validate_url(url, source)
    except Exception:
        raise CollectorUnavailable('Configure explicit source/category search URLs') from None
    owned = transport is None
    transport = transport or Transport(source)
    rows, seen, queue = {}, set(), list(seeds)
    page_limit = max(1, min(settings.scraper_max_pages, 30))
    fetched = 0
    cap = max(1, min(int(cap), 1000))
    try:
        while queue and fetched < page_limit and len(rows) < cap:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            text = transport.get(url)
            fetched += 1
            extracted = page_data.extract(text, source)
            if not extracted and source == 'homes':
                from portals.rendered import property_links
                discovered = property_links(text, url)
                if discovered:
                    for target in discovered:
                        if target not in seen and target not in queue:queue.append(target)
                    continue
            if not extracted:
                raise CollectorUnavailable('No recognisable listing data; source adapter needs checking')
            for record_url, raw in extracted.items():
                if raw.get('_apex_kind', kind) != kind:
                    continue
                validate_url(record_url, source)
                row = canonical(source, kind, record_url, raw)
                if str(row.get('region', '')).strip().casefold() != 'auckland':
                    continue
                if suburb and page_data.text_key(row.get('suburb', '')) != page_data.text_key(suburb):
                    continue
                if row.get('address'):
                    rows[record_url] = row
                if len(rows) >= cap:
                    break
            for target in next_pages(text,url,source):
                if target not in seen and target not in queue:queue.append(target)
        # Discover search pages first. Detail enrichment must not spend the
        # entire request budget on the first page and starve pagination.
        pending_details = []
        for record_url, row in rows.items():
            if record_url in seen:
                continue
            if fetched >= page_limit:
                pending_details.append(record_url)
                continue
            detail_text = transport.get(record_url)
            fetched += 1
            seen.add(record_url)
            detail = page_data.extract(detail_text, source).get(record_url)
            if detail is None:
                raise CollectorUnavailable('Listing detail schema changed or canonical URL missing')
            enriched = canonical(source, kind, record_url, detail)
            conflicts = {k: [row[k], value] for k, value in enriched.items()
                         if page_data.present(row.get(k)) and page_data.present(value)
                         and row[k] != value and k not in ('raw_source', 'provenance', 'scraped_at')}
            for key, value in enriched.items():
                if not page_data.present(row.get(key)):
                    row[key] = value
                    if key in enriched['provenance']:
                        row['provenance'][key] = enriched['provenance'][key]
            row['raw_source'] = {'search': row['raw_source'], 'detail': detail}
            row['source_conflicts'] = conflicts
        for row in rows.values():
            row['collection_scope'] = {'pages_fetched': fetched, 'page_limit': page_limit, 'record_cap': cap, 'pending_search_urls':list(queue), 'pending_detail_urls':pending_details, 'complete_coverage_verified': False}
        return list(rows.values())
    finally:
        if owned:
            transport.close()


def merge_records(rows):
    """Merge exact address+unit+suburb+district only; no fuzzy property joins.

    Same property may have separate sale, rental and dated sold records. Full
    source snapshots survive; missing fields fill and disagreements are retained.
    """
    grouped = {}
    for row in rows:
        key = page_data.match_key(row)
        group = (key or (row['source'], row['url']), row['kind'], row.get('sold_date') if row['kind']=='sold' else None)
        grouped.setdefault(group, []).append(row)
    result = []
    metadata = {'source','url','source_id','scraped_at','raw_source','provenance','collection_scope','source_conflicts','conflicts','_apex_direct'}
    for group in grouped.values():
        merged = dict(group[0]); provenance = {}; conflicts = {}
        for row in group:
            if row.get('conflicts'):
                conflicts.setdefault('upstream',[]).append(row['conflicts'])
            origin = {'source': row['source'], 'url': row['url'], 'collected_at': row.get('scraped_at')}
            for key,value in row.items():
                if key in metadata or not page_data.present(value):continue
                if not page_data.present(merged.get(key)):
                    merged[key]=value
                if merged[key]==value:
                    provenance.setdefault(key,[]).append(origin)
                else:
                    conflicts.setdefault(key,[]).append({'value':value,**origin})
        merged['provenance']=provenance
        merged['conflicts']=conflicts
        merged['source_snapshots']=group
        # Keep the stable first-source identity for existing review dedupe. All
        # contributing source identities and raw fields remain in raw_json.
        if conflicts or any(r.get('source_conflicts') for r in group):
            merged['price_flag']='Source records disagree — review source evidence before approval'
        result.append(merged)
    return result
