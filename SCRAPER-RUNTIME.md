# Direct collector browser runtime

Homes map discovery needs JavaScript. Install the optional worker dependencies:

```
pip install -r requirements-scraper.txt
python -m playwright install --with-deps chromium --only-shell
```

Set PLAYWRIGHT_BROWSERS_PATH to a persistent installed browser path if build/runtime users differ. Enable SCRAPER_RENDER_HOMES only in the collection worker after its supervised test. Keep SCRAPER_ENABLED and SCRAPER_DAILY_PRICING off until full collection/review/repricing validation completes. These changes do not provision a new service or alter the API build.

The renderer uses a fresh unauthenticated browser context with the same selected proxy as the HTTP collector. Robots and access checks precede rendering; restrictions fail without changing proxies. Images/fonts/video are skipped. Map links are discovery candidates only: property details supply sale facts and the existing validation/merge path decides eligibility. Empty/unloaded searches are errors, not evidence of delisting. Request limits still apply and complete Auckland coverage is not yet verified.
