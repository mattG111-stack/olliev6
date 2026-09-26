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


def render_homes(url, *, proxy=None, timeout_ms=30000, max_batches=3):
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
    html=rendered_html(page,url,timeout_ms,restricted)
    discovered = dict.fromkeys(property_links(html, url))
    displayed=page.locator('body').inner_text()
    match=re.search(r'([\d,]+)\s+properties\b',displayed,re.I)
    total=int(match[1].replace(',','')) if match else None
    for _ in range(max(0,min(int(max_batches),10)-1)):
        count=len(discovered)
        if total is None or count>=total:break
        container=page.locator('.drawerContentContainer')
        if container.count()!=1:break
        container.evaluate('(el) => { el.scrollTop=el.scrollHeight; }')
        try:
            page.wait_for_function("""known =>
                /verify you are human|access denied|captcha/i.test(document.body.innerText) ||
                [...document.querySelectorAll('a[href*="/address/auckland/"]')]
                    .some(a => !known.includes(a.href.split('?')[0].split('#')[0]))
            """,arg=list(discovered),timeout=min(timeout_ms,10000))
        except Exception:
            # An unchanged window is partial coverage, never an empty region.
            html=rendered_html(page,url,timeout_ms,restricted)
            discovered.update(dict.fromkeys(property_links(html,url)))
            break
        html=rendered_html(page,url,timeout_ms,restricted)
        discovered.update(dict.fromkeys(property_links(html,url)))
    # Virtualized lists replace earlier cards. Keep the exact observed URLs,
    # including those no longer present in the final DOM, for detail fetching.
    html += ''.join(f'<a href="{escape(link, quote=True)}" data-apex-discovered="true"></a>' for link in discovered)
    from portals.direct import CollectorUnavailable, MAX_BYTES
    if len(html.encode()) > MAX_BYTES:
        raise CollectorUnavailable('Rendered discovery evidence exceeds collection size limit')
    count=len(discovered)
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


def rendered_html(page, url, timeout_ms, restricted):
    """Wait for public listing DOM; never interpret a spinner as zero stock."""
    from portals.direct import CollectorUnavailable, MAX_BYTES
    page.wait_for_function("""() => {
        const body=document.body.innerText;
        return /captcha|verify you are human|access denied/i.test(body) ||
            (!document.querySelector('[role="progressbar"]') &&
             (document.querySelector('a[href*="/address/auckland/"]') || /0 properties|no properties found/i.test(body)));
    }""",timeout=timeout_ms)
    html=page.content()
    if restricted or any(v in html.lower() for v in ('cf-chl-','g-recaptcha','hcaptcha','verify you are human','access denied')):
        raise CollectorUnavailable('Homes rendering stopped: access restriction')
    if len(html.encode())>MAX_BYTES:raise CollectorUnavailable('Rendered page exceeds collection size limit')
    if not property_links(html,url):
        raise CollectorUnavailable('Homes rendered search has no usable listing links; scope is unverified')
    return html
