"""Optional isolated browser rendering for public Homes map pages.

No user browser profile, login, challenge solving or proxy retry. The caller
must perform its normal robots/access checks before rendering the same URL.
"""
from urllib.parse import urlsplit, unquote, urljoin
from html.parser import HTMLParser
from html import escape
import re


def property_links(html, base_url):
    from portals.direct import validate_url
    links=[]
    class Links(HTMLParser):
        def handle_starttag(self, tag, attrs):
            href=dict(attrs).get('href')
            if tag!='a' or not href:return
            url=urljoin(base_url,href)
            u=urlsplit(url)
            if u.hostname not in ('homes.co.nz','www.homes.co.nz') or not u.path.startswith('/address/auckland/'):
                return
            validate_url(url,'homes')
            url=u._replace(query='',fragment='').geturl()
            if url not in links:links.append(url)
    Links().feed(html)
    return links


def proxy_options(url):
    if not url:return None
    u=urlsplit(url)
    if u.scheme not in ('http','https') or not u.hostname:raise ValueError('Invalid proxy configuration')
    result={'server':f'{u.scheme}://{u.hostname}'+(f':{u.port}' if u.port else '')}
    if u.username is not None:result['username']=unquote(u.username)
    if u.password is not None:result['password']=unquote(u.password)
    return result


def render_homes(url, *, proxy=None, timeout_ms=30000, max_batches=50):
    from portals.direct import CollectorUnavailable, validate_url, USER_AGENT, MAX_BYTES
    validate_url(url,'homes')
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True, proxy=proxy_options(proxy))
            try:
                context=browser.new_context(user_agent=USER_AGENT, service_workers='block')
                page=context.new_page()
                restricted=[]
                def route_request(route):
                    req=route.request
                    if req.is_navigation_request() and req.frame==page.main_frame:
                        try:validate_url(req.url,'homes')
                        except CollectorUnavailable:
                            restricted.append('navigation');route.abort();return
                    if req.resource_type in ('image','media','font'):
                        route.abort()
                    else:route.continue_()
                def response_seen(response):
                    host=urlsplit(response.url).hostname or ''
                    if host=='homes.co.nz' or host.endswith('.homes.co.nz'):
                        if response.status in (401,403,429) or response.headers.get('x-amzn-waf-action') in ('challenge','captcha'):
                            restricted.append('access')
                page.route('**/*',route_request)
                page.on('response',response_seen)
                response=page.goto(url,wait_until='domcontentloaded',timeout=timeout_ms)
                if response is None or response.status!=200 or restricted:
                    raise CollectorUnavailable('Homes rendering stopped after an unsuccessful response')
                return load_homes_results(page, url, timeout_ms, restricted, max_batches=max_batches)
            finally:browser.close()
    except CollectorUnavailable:raise
    except Exception:
        # Browser exceptions can include proxy credentials and request URLs.
        raise CollectorUnavailable('Homes browser unavailable or page did not finish loading') from None


def load_homes_results(page, url, timeout_ms, restricted, *, max_batches=3):
    """Load bounded public result batches; retain the displayed coverage count."""
    html=rendered_html(page,url,timeout_ms,restricted,True)
    discovered = dict.fromkeys(property_links(html, url))
    displayed=page.locator('body').inner_text()
    match=re.search(r'([\d,]+)\s+properties\b',displayed,re.I)
    total=int(match[1].replace(',','')) if match else None
    # Retain every observed window before advancing. Leaving the bottom zone
    # rearms the public drawer's load trigger before entering it again.
    # A stalled cursor is partial coverage, never evidence of completion.
    for _ in range(max(0,min(int(max_batches),500)-1)):
        count=len(discovered)
        if total is None or count>=total:break
        container=page.locator('.drawerContentContainer')
        if container.count()!=1:break
        before=container.evaluate('''el => ({top:el.scrollTop, height:el.scrollHeight,
            viewport:el.clientHeight})''')
        container.evaluate('async el => {\n            el.scrollTop=0;\n            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));\n            el.scrollTop=el.scrollHeight;\n        }')
        try:
            page.wait_for_function("""({known, base}) =>
                /verify you are human|access denied|captcha/i.test(document.body.innerText) ||
                [...document.querySelectorAll('a[href*="/address/auckland/"]')]
                    .some(a => !known.includes(new URL(a.getAttribute('href'), base).href.split('?')[0].split('#')[0]))
            """,arg={'known':list(discovered), 'base':url},timeout=min(timeout_ms,10000))
        except Exception:
            # Capture the final window after a timeout; no new identities
            # means partial coverage, never an empty or completed region.
            html=rendered_html(page,url,timeout_ms,restricted,True)
            discovered.update(dict.fromkeys(property_links(html,url)))
        else:
            html=rendered_html(page,url,timeout_ms,restricted,True)
            discovered.update(dict.fromkeys(property_links(html,url)))
        after=container.evaluate('el => ({top:el.scrollTop, height:el.scrollHeight})')
        if after['top']==before['top'] and after['height']==before['height'] and len(discovered)==count:
            break
    # Virtualized lists replace earlier cards. Keep the exact observed URLs,
    # including those no longer present in the final DOM, for detail fetching.
    html += ''.join(f'<a href="{escape(link, quote=True)}" data-apex-discovered="true"></a>' for link in discovered)
    from portals.direct import CollectorUnavailable, MAX_BYTES
    if len(html.encode()) > MAX_BYTES:
        raise CollectorUnavailable('Rendered discovery evidence exceeds collection size limit')
    count=len(discovered)
    # The public result count can change while a long scan is running.
    # Never certify coverage against an obsolete, smaller initial count.
    final_match=re.search(r'([\d,]+)\s+properties\b',page.locator('body').inner_text(),re.I)
    total=int(final_match[1].replace(',','')) if final_match else None
    complete=total is not None and count>=total
    return html+f'<meta name="apex-homes-coverage" data-loaded="{count}" data-total="{total if total is not None else "unknown"}" data-complete="{str(complete).lower()}">'


def discovery_info(html):
    info={}
    class Meta(HTMLParser):
        def handle_starttag(self,tag,attrs):
            a=dict(attrs)
            if tag=='meta' and a.get('name')=='apex-homes-coverage':
                info.update(loaded=int(a['data-loaded']),
                    total=int(a['data-total']) if a['data-total'].isdigit() else None,
                    complete=a.get('data-complete')=='true')
    Meta().feed(html)
    return info


def rendered_html(page, url, timeout_ms, restricted, discovery_only=False):
    """Wait for public listing DOM; never interpret a spinner as zero stock."""
    from portals.direct import CollectorUnavailable, MAX_BYTES
    page.wait_for_function("""() => {
        const body=document.body.innerText;
        return /captcha|verify you are human|access denied/i.test(body) ||
            (!document.querySelector('[role="progressbar"]') &&
             (document.querySelector('a[href*="/address/auckland/"]') || /0 properties|no properties found/i.test(body)));
    }""",timeout=timeout_ms)
    if discovery_only:
        # Search discovery needs observed public URLs, not thousands of image
        # carousel nodes. Individual detail evidence is still fetched/stored.
        # Limit the compact evidence too; never disable the transport guard.
        snapshot=page.evaluate('''() => ({
            challengeText: /verify you are human|access denied|captcha/i.test(document.body.innerText),
            challengeWidget: !!document.querySelector('[class*="cf-chl-"],.g-recaptcha,[class*="hcaptcha"],iframe[src*="captcha"]'),
            links: [...new Set([...document.querySelectorAll('a[href*="/address/auckland/"]')].map(a => a.href))]
        })''')
        if restricted:
            raise CollectorUnavailable('Homes rendering stopped: access restriction (source response)')
        if snapshot['challengeText']:
            raise CollectorUnavailable('Homes rendering stopped: access restriction (visible challenge text)')
        if snapshot['challengeWidget']:
            raise CollectorUnavailable('Homes rendering stopped: access restriction (challenge widget)')
        html=''.join(f'<a href="{escape(link,quote=True)}">Observed property</a>' for link in snapshot['links'])
    else:
        html=page.content()
    if restricted or any(v in html.lower() for v in ('cf-chl-','g-recaptcha','hcaptcha','verify you are human','access denied')):
        raise CollectorUnavailable('Homes rendering stopped: access restriction')
    if len(html.encode())>MAX_BYTES:raise CollectorUnavailable('Rendered page exceeds collection size limit')
    if not property_links(html,url):
        raise CollectorUnavailable('Homes rendered search has no usable listing links; scope is unverified')
    return html


def trademe_property_links(html, base_url):
    from portals.direct import validate_url
    links=[]
    class Links(HTMLParser):
        def handle_starttag(self,tag,attrs):
            href=dict(attrs).get('href')
            if tag!='a' or not href:return
            url=urljoin(base_url,href);u=urlsplit(url)
            if u.hostname not in ('trademe.co.nz','www.trademe.co.nz'):return
            if not re.fullmatch(r'/a/property/residential/sale/auckland/[^/]+/[^/]+/listing/\d+',u.path):return
            validate_url(url,'trademe')
            url=u._replace(query='',fragment='').geturl()
            if url not in links:links.append(url)
    Links().feed(html)
    return links


def trademe_search_html(page, url, timeout_ms, restricted):
    from portals.direct import CollectorUnavailable, MAX_BYTES, next_pages
    if restricted:raise CollectorUnavailable('Trade Me rendering stopped: access restriction')
    # A source outage has no listing anchors. Recognise its visible error
    # instead of waiting for a listing and reporting an ambiguous timeout.
    page.wait_for_function('''() => document.querySelector('a[href*="/listing/"]')
        || /verify you are human|access denied|captcha|unable to retrieve search results/i.test(document.body.innerText)
        || document.querySelector('[class*="cf-chl-"],.g-recaptcha,[class*="hcaptcha"],iframe[src*="captcha"]')''',timeout=timeout_ms)
    snapshot=page.evaluate('''() => ({
        blocked: /verify you are human|access denied|captcha/i.test(document.body.innerText)
          || !!document.querySelector('[class*="cf-chl-"],.g-recaptcha,[class*="hcaptcha"],iframe[src*="captcha"]'),
        unavailable: /unable to retrieve search results/i.test(document.body.innerText),
        links: [...document.querySelectorAll('a[href]')].map(a => ({url:a.href,label:a.getAttribute('aria-label')||'',text:a.innerText}))
    })''')
    if restricted or snapshot['blocked']:raise CollectorUnavailable('Trade Me rendering stopped: access restriction')
    if snapshot['unavailable']:raise CollectorUnavailable('Trade Me search is temporarily unavailable at the source')
    html=''.join(f'<a href="{escape(a["url"],quote=True)}" aria-label="{escape(a["label"],quote=True)}">{escape(a["text"])}</a>' for a in snapshot['links'])
    details=trademe_property_links(html,url)
    if not details:raise CollectorUnavailable('Trade Me rendered search has no usable listing links')
    # Only exact observed Auckland listing links and the next filtered page.
    compact=''.join(f'<a href="{escape(u,quote=True)}">Observed property</a>' for u in details)
    compact+=''.join(f'<a href="{escape(u,quote=True)}" aria-label="Next page">Next</a>' for u in next_pages(html,url,'trademe'))
    if len(compact.encode())>MAX_BYTES:raise CollectorUnavailable('Rendered page exceeds collection size limit')
    return compact


def render_trademe_search(url, *, proxy=None, timeout_ms=30000):
    from portals.direct import CollectorUnavailable, validate_url, USER_AGENT
    validate_url(url,'trademe')
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True,proxy=proxy_options(proxy))
            try:
                context=browser.new_context(user_agent=USER_AGENT,service_workers='block')
                page=context.new_page();restricted=[]
                def route_request(route):
                    req=route.request
                    if req.is_navigation_request() and req.frame==page.main_frame:
                        try:validate_url(req.url,'trademe')
                        except CollectorUnavailable:
                            restricted.append('navigation');route.abort();return
                    if req.resource_type in ('image','media','font'):route.abort()
                    else:route.continue_()
                def response_seen(response):
                    host=urlsplit(response.url).hostname or ''
                    if host=='trademe.co.nz' or host.endswith('.trademe.co.nz'):
                        if response.status in (401,403,429) or response.headers.get('x-amzn-waf-action') in ('challenge','captcha'):
                            restricted.append('access')
                page.route('**/*',route_request);page.on('response',response_seen)
                response=page.goto(url,wait_until='domcontentloaded',timeout=timeout_ms)
                if response is None or response.status!=200 or restricted:
                    raise CollectorUnavailable('Trade Me rendering stopped after an unsuccessful response')
                return trademe_search_html(page,url,timeout_ms,restricted)
            finally:browser.close()
    except CollectorUnavailable:raise
    except Exception:raise CollectorUnavailable('Trade Me browser unavailable or page did not finish loading') from None
