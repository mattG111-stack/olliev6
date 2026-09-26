# Direct collector browser runtime

## Dedicated Railway worker

Build `Dockerfile.scraper` and run its default `python scraper_worker.py` command.
This entry point includes only direct new-listing collection, sold collection and
validated daily pricing. It excludes unrelated legacy maintenance jobs.
Use the existing PostgreSQL database through a Railway service reference. Keep
SCRAPER_ENABLED, PORTALS_DAILY and SCRAPER_DAILY_PRICING false for initial deployment.
The worker has no HTTP healthcheck or public domain. Run the collection migration
in the backend deployment before enabling collection. Proxy credentials belong in
private service variables, never source files.

Saved partial passes resume on the next five-minute scheduler check. A completed
pass retains its daily success timestamp across restarts. This is durable resume,
not yet a verified newest-listing cutoff across all three sources.

Homes map discovery needs JavaScript. Install the optional worker dependencies:

```
pip install -r requirements-scraper.txt
python -m playwright install --with-deps chromium --only-shell
```

Set PLAYWRIGHT_BROWSERS_PATH to a persistent installed browser path if build/runtime users differ. Enable SCRAPER_RENDER_HOMES only in the collection worker after its supervised test. Keep SCRAPER_ENABLED and SCRAPER_DAILY_PRICING off until full collection/review/repricing validation completes. These changes do not provision a new service or alter the API build.

The renderer uses a fresh unauthenticated browser context with the same selected proxy as the HTTP collector. Robots and access checks precede rendering; restrictions fail without changing proxies. Images/fonts/video are skipped. Map links are discovery candidates only: property details supply sale facts and the existing validation/merge path decides eligibility. Empty/unloaded searches are errors, not evidence of delisting. Request limits still apply and complete Auckland coverage is not yet verified.
