"""One bounded public-page smoke check; never imports or writes property data."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from config import settings
from portals.direct import Transport, CollectorUnavailable, USER_AGENT
from portals.rendered import property_links

settings.scraper_render_homes=True
# Observed from the public search UI, not a guessed internal endpoint.
url='https://homes.co.nz/map/auckland/mount-wellington/mount-wellington-highway?searchLoc=tsh%60Fmocj%60@&filter=type:sold'
transport=Transport('homes',client=httpx.Client(timeout=30,follow_redirects=False,headers={'User-Agent':USER_AGENT}))
try:
    html=transport.get(url)
    links=property_links(html,url)
    print(json.dumps({'ok':True,'discovered':len(links),'scope':'sold near Mount Wellington Highway','production_writes':False}))
except CollectorUnavailable as exc:
    print(json.dumps({'ok':False,'reason':str(exc),'production_writes':False}))
    sys.exit(1)
finally:transport.close()
