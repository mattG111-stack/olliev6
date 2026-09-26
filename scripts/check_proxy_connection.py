"""One read-only proxy connection check, without enabling collection or a database.

Run inside the configured worker: python scripts/check_proxy_connection.py
Reads SCRAPER_PROXY_URLS from the environment; never prints it or response data.
This verifies transport only, not permission/access to any property source.
"""
import json
import os
from urllib.parse import urlsplit
import httpx


def check(raw):
    try:
        urls=json.loads(raw or '[]')
        if not isinstance(urls,list) or not urls or not isinstance(urls[0],str):
            raise ValueError()
        parsed=urlsplit(urls[0])
        if parsed.scheme not in ('http','https') or not parsed.hostname:raise ValueError()
        parsed.port  # Validate without displaying the connection string.
    except (ValueError,TypeError):
        return {'ok':False,'reason':'Missing or invalid proxy configuration'}
    try:
        with httpx.Client(proxy=urls[0],timeout=15,follow_redirects=False,trust_env=False) as client:
            # Fixed provider diagnostic endpoint. No listing collection,
            # challenge handling, retries, rotations or production writes.
            with client.stream('GET','https://ip.decodo.com/json') as response:
                code=response.status_code
        return {'ok':code==200,'http_status':code,'checked_proxy_index':0,
                'source_access_verified':False,'production_writes':False}
    except Exception:
        # Client exceptions may contain proxy usernames/passwords.
        return {'ok':False,'reason':'Proxy connection failed; check provider and server configuration',
                'source_access_verified':False,'production_writes':False}


if __name__=='__main__':
    result=check(os.environ.get('SCRAPER_PROXY_URLS',''))
    print(json.dumps(result))
    raise SystemExit(0 if result['ok'] else 1)
