# Ollie launch review — 1.87-review.1

Reviewed on 22 September 2026. This is a tested source candidate, not a deployed release or a guarantee that every defect has been found.

## Source and repository reconciliation

The two supplied v1.87 archives contain six changed files, not complete applications. The application was reconstructed from the v1.86 split archives in Downloads, overlaid with v1.87. The older backend test suite was recovered from the v1.84 full archive and updated for the flattened Python layout.

The supplied GitHub repositories were cloned and compared:

- Backend: https://github.com/mattG111-stack/olliev6 — commit `1ff6e0a9dcd3e3bb73fe7e184b5ba016f4270d7d`.
- Frontend: https://github.com/mattG111-stack/ollie-v5-frontend — commit `f511dff7c5823822e2800408b833153b4fa74f20`.

Both identify themselves as v1.86. Their application source matches the reconstructed base; the backend also has its migrations incorrectly named `alembic 2`. That folder is restored to `alembic`, matching `alembic.ini`. The separate v1.88 build mentioned in the conversation was not supplied and was not evaluated. `WORKING_STATUSES` remains defined, its callers are covered, and the reviewed backend boots.

## Corrected defects

| Area | Finding and correction |
| --- | --- |
| Publish selection | Review, preview, enrichment, repricing and portal approval could choose different batches when a newer upload was empty. They now use the same populated-batch selector. |
| Repeat publication | An empty duplicate or older staged snapshot remained eligible after publishing. For-sale leftovers become `superseded`; sold history remains readable. Repeated publication no longer empties or rolls back the feed. |
| Concurrent publication | PostgreSQL publication is serialized with a transaction advisory lock for the region. This branch requires PostgreSQL staging verification; local tests use SQLite. |
| Publication audit | The HTTP publication and its job record commit in the same transaction; a logging failure can roll back the publication. |
| Restore | A newer archived record marked gone now prevents restoring an older apparently live copy. The restore count and action agree. |
| Batch deletion | Kept listings go to a previously published batch rather than an unreviewed upload. Reactivation sets the batch back to `published`. Deleting a draft no longer moves its rows into the live feed. |
| Review dashboard | Response validation discarded live counts, priced/unpriced counts and weekly removals. These fields are now returned. Listings both held and gone are counted once in the category breakdown. |
| Weekly comparison | Customer changes no longer compare a live batch against a newer, unpublished upload. |
| Dollar margins | The listing and deal cards, dollar sorting, and summary medians no longer recreate a margin the pricing engine deliberately suppressed. |
| Map empty state | Missing coordinates are no longer described as proof that no listings match. The message directs users to List view. |
| Billing security | Missing Stripe signing configuration no longer makes the webhook accept unsigned payloads. Missing signatures are rejected. |
| Administrator setup | Startup no longer creates/resets a seed admin using the shipped default credentials. Explicit credentials are required for seeding. Existing accounts are not deleted. |
| Verification | SQLite timestamps are normalized to UTC before comparison, fixing verification-code errors in local/testing environments. |
| Dependency security | Next.js upgraded to 15.5.24; vulnerable Python framework, multipart, dotenv and cryptography packages updated. JWT handling moved from python-jose to PyJWT, removing the vulnerable dependency chain. A hashed Python dependency lock is supplied. |
| Browser-test reliability | Backend location is configurable, the test database is separate for each run, and database deletion requires explicit test-reset opt-in. The publishing test accepts the confirmation dialog, checks the POST result, verifies the actual new listings as a customer and checks that no batch remains staged. It cannot pass merely because an older batch says active. |
| Test coverage | Recovered tests now import the actual root modules, use disposable databases, include the adjacent frontend checks, and inspect effective FastAPI routes including inherited authentication dependencies. |

## Validation

- **2,051 backend tests passed; 10 skipped.** See `backend-tests.txt` and `backend-tests.xml` in the evidence archive.
- Full production frontend build passed on Next.js 15.5.24, including TypeScript checking. A final separate TypeScript check covers the last test-configuration changes.
- Python runtime package audit: **zero known vulnerabilities** at audit time. JavaScript audit: **zero known vulnerabilities** at audit time. These are package audits, not proof that application security is complete.
- Hashed Python lock installed successfully against the tested Python 3.12 environment.
- Alembic resolves one head, `a1c2e3d4f5b6`, from the corrected directory. No production migration was executed.
- Direct browser checks against local disposable data: administrator sign-in, populated review grid, preview, confirmation and publish, `Nothing staged` after publish, customer sign-in, both newly published listings visible in List view, corrected withheld-margin display, and property detail navigation.
- The automated Playwright run could not launch Chromium in this macOS sandbox (process exited with SIGTRAP). It did **not** pass. Direct browser checks cover the critical publication flow, not the entire browser suite or every viewport.

The 10 skips are explicit: one PostgreSQL-only endpoint, one absent real Trade Me export, five valuation tests needing bedroom data not present in the fixture, and three inapplicable negative-profit cases. PostgreSQL-only SQL, production-volume performance and valuation accuracy on the actual current dataset still require verification.

## Remaining launch requirements

1. Grant GitHub write access to both repositories. Local branches are prepared as `codex/launch-review-20260922`; no code has been pushed, merged or deployed from this review.
2. Identify the Railway project and backend/frontend services. Their current configuration, production code and database have not been inspected.
3. Use a database backup and a staging copy to exercise the existing bootstrap/migration behavior, publication, rollback and deletion on PostgreSQL. This app changes schema during startup; no production schema claim can be made from SQLite tests.
4. Verify `DATABASE_URL`, the existing `JWT_SECRET`, explicit seed-admin settings if seeding is wanted, frontend `BACKEND_ORIGIN` at build time, `APP_BASE_URL`, and CORS settings. Keep the existing JWT secret to preserve existing token/key compatibility. Remove or rotate any legacy default administrator account if it was previously created.
5. Validate Stripe in test mode with real signed webhooks, including subscription changes; verify Resend and Twilio delivery for onboarding. These services were not contacted using production credentials. Webhooks now refuse to run without `STRIPE_WEBHOOK_SECRET`.
6. Run the full browser suite in an environment that can launch Chromium. Test mobile layouts, real photos/maps, and the actual portal/LLM feeds with appropriate service configuration. Paid integrations were not exercised.
7. Deploy both reviewed versions together, then check version endpoints/dashboard, health, login, customer listings and the publish flow on staging before exposing the production change.

## Applying the deliverables

The full ZIPs contain source and configuration, including complete test code; they exclude environments, node_modules, build output, local databases and secrets. Extract them as sibling `backend/` and `frontend/` directories. Railway must build the source; the ZIPs are not prebuilt server images.

Alternatively, apply each `.patch` inside a clean checkout at the corresponding commit above using `git apply --index /path/to/the.patch`. The patches include new files and the migration-directory correction. Review with `git diff --cached` before committing. Do not blindly overlay them over a newer unrelated release.

Backend validation:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install pytest
python -m pytest tests
```

Frontend validation:

```sh
npm ci
BACKEND_ORIGIN=http://127.0.0.1:8100 npm run build
```

Browser testing, with the backend virtualenv on PATH and both source folders present:

```sh
npx playwright install chromium
E2E_BACKEND_DIR=../backend npm run e2e
```

The browser harness creates its own disposable SQLite database and sets `E2E_DATABASE_RESET=1`. Never run the seed script against a real database. For a deployed backend, use the existing root-module startup (`main:app`), not the obsolete `app.main:app` in the original README. Use the hashed lock for reproducible dependency installation.
