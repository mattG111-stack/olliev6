# Apex Property — build log

Every build bumps the version by 0.1 in **both** repositories, and the admin
dashboard shows the two side by side.

The number exists so a bug report can be checked against a build. "That was
fixed" and "it is still broken" are both easy to say and, until now, impossible
to reconcile: the two halves deploy separately, and a backend can sit days
behind its frontend with nothing on screen saying so. Every symptom then reads
as a new bug rather than an old one that was fixed but never deployed.

Check the running build at **Admin → Business & data dashboard**, or hit
`/api/version` directly — that one needs no login, because the times you most
need it include the times nobody can sign in.

---

## v1.8 — 2026-08-15

**Now it collects the deliberate failures too**
- v1.7's handler only saw CRASHES. Every error this codebase raises on purpose —
  "assistant settings are unavailable: UndefinedTable", "could not delete:
  FOREIGN KEY constraint failed" — is an HTTPException, which FastAPI handles, so
  none of them reached the log. Those are the most useful entries of all:
  someone already worked out what went wrong and wrote it down. 5xx are now
  logged with that message.
- 4xx are deliberately NOT logged. A 401 on an expired token, a 404 on a stale
  link, a 422 on a mistyped form — that is the application working, and filing
  them would bury the real faults.
- The browser reports a fault when the API answers 5xx **or does not answer at
  all**. A server that is down or unreachable is the one failure it can never
  record about itself, and it is exactly the one that looks like "none of it
  works". De-duplicated per session so a retry loop cannot flood.
- Nothing under /api/bugs is ever reported, on either side — a reporter that
  files faults about itself is an unbounded loop with the log as its output.

---

## v1.7 — 2026-08-15

**Bugs file themselves**
- Every unhandled server error is now recorded in the bug log with the endpoint,
  the exception and the traceback. Until now a 500 existed only in a log nobody
  was reading, so a fault was known about exactly as often as someone noticed it
  and said so. The caller still gets a plain 500 and the traceback stays
  server-side.
- Crashes in the page send themselves too — message, stack and page, with the
  build attached. Every round of debugging this app has started with a console
  error pasted into a chat, which only happens when someone has devtools open
  and thinks to copy it.
- Repeats are counted on one entry rather than filed again. One broken endpoint
  clicked ten times is one fault; a log that floods is a log nobody opens.
  Browser crashes fingerprint on the message and page, not the stack, so a
  rebuilt bundle does not re-file every existing crash.
- A fault that recurs after being marked fixed opens a NEW entry rather than
  reviving the closed one, so a regression is visible instead of being folded
  back into something already reviewed.
- Auto-filed rows are badged AUTO in the table, with a repeat count and a
  last-seen time; the CSV carries both.

**Not covered, honestly:** a failed Railway BUILD cannot be captured here — the
app is not running when a build fails. The version panel is what tells you a
deploy did not land: if the API still reports the old number after an upload,
the build failed.

---

## v1.6 — 2026-08-15

**Bug log (Admin → Bug log)**
- File a fault in one line. The form attaches what actually matters and nobody
  would type: the app build, the API's own build (taken from the server
  answering, never from the browser — a mismatch between the two is itself a
  common cause), the page, the browser, and the last ten failed requests with
  the server's own message.
- Status and severity per row, a note for what was found, and **Download CSV**
  with the captured errors flattened into one readable column.
- A build mismatch on a report is flagged in the table: it means the two halves
  were not the same code when the fault happened, which changes what the report
  means.
- Any signed-in user can file one; only an admin can read, edit or export the
  log. The person who hits a fault is rarely the person with the admin password.
- Request bodies are never captured, so a password or API key typed into a form
  cannot end up in a bug report.

---

## v1.5 — 2026-08-15

**Creating users and setting passwords**
- The account lookup used `trim()` inside SQL. `lower()` means the same thing on
  every database; `trim()` does not — the standard spells it `trim(BOTH FROM x)`
  and dialects differ on whether a bare `trim(x)` is a function at all. That put
  sign-in AND every admin user action on a construct that may not exist on the
  database actually running. Now `lower()` only, with a small in-Python fallback
  for stray whitespace.
- `hash_password` could raise, and creating a user, setting a password and
  signing up all call it — so a bcrypt problem surfaced as 500 on exactly those
  three, and as 401 on every login (verification swallows errors and answers
  "wrong password"). None of those symptoms mention bcrypt. It now raises a
  typed error carrying the library and the message, and those endpoints return
  503 naming it.
- A broken bcrypt also stopped the server STARTING, because the boot-time seed
  admin repair hashes a password and anything it raises aborts the lifespan. A
  crash loop cannot tell anyone why. Boot repairs are now non-fatal.
- `/api/admin/diagnostics` reports the bcrypt and passlib versions and whether
  this server can hash and verify a password, and the admin dashboard has a
  **Diagnostics** button that shows the whole report inline — safe to screenshot.

---

## v1.4 — 2026-08-15

**The reason the last few builds shipped bugs**
- The tests run on SQLite; production runs Postgres. **SQLite ignores every
  foreign key unless explicitly told not to**, and it was not told. So a delete
  that left a row pointing at a vanished user passed every test and failed in
  production with a constraint violation the tests could not have seen. That is
  exactly how `app_settings.updated_by` shipped uncleared and made deleting an
  admin answer 500.
- Enforcement is now on for SQLite, so the test database refuses what the real
  one refuses. Turning it on immediately failed three test fixtures that had been
  leaving orphaned rows behind — the same fault, in the tests themselves.

---

## v1.3 — 2026-08-15

**The admin panel 500s**
- `/api/admin/assistant/key`, `/usage` and `/api/admin/users/{id}` answered 500.
  A 500 in a browser console carries nothing and the person reading it cannot see
  the server log, so every round cost a deploy cycle to guess at. Those endpoints
  now either recover or say what is actually wrong.
- `app_settings` is created on demand if it is missing. `db_bootstrap` runs
  `create_all` on every boot, but that call is wrapped in a catch-all — when it
  fails, the first symptom is every assistant endpoint 500ing and the cause is
  only in a boot log nobody is reading by then.
- A failed statement leaves a Postgres transaction unusable, so one missing table
  made every later query in the same request fail with a misleading error. Every
  swallowed exception now rolls back.
- Deleting a user cleans up each dependent table in its own savepoint, so a table
  that does not exist on an older database no longer takes the delete with it.
  `app_settings.updated_by` was a foreign key to users that the delete did not
  clear — an admin who had saved an API key could not be deleted.
- New `GET /api/admin/diagnostics` (admin only): the build, the database engine,
  which tables the models expect that are missing, and the real error per admin
  feature. Table names and row counts only — no row contents, no credentials, and
  never the database URL.

---

## v1.2 — 2026-08-15

**Validation**
- `max_addl_lots` meant two different things depending on which pro-forma ran.
  The THAB terrace path returned the TOTAL terrace count where every other path
  returns `sections - 1`, so each THAB row was over by exactly one — a 100%
  overstatement on a two-lot site. This is the shape of the **+52.57% bias** the
  report has shown on that output every single run.
- The validation report now segments the subdivision outputs by which pro-forma
  produced each row, and prints the exact integer difference distribution for
  lot counts. "+207% on net gain" is a number, not a diagnosis; a blended figure
  across two completely different pro-formas cannot say which one is wrong.

---

## v1.1 — 2026-08-15

**Sign-in**
- Accounts whose stored email had capitals or a stray space could never sign in.
  The lookup lowercased the input and compared it to the column as written, so
  no input could match; the account answered 401 with the correct password.
  Lookups are now case- and space-insensitive, and a boot repair normalises
  existing rows (the boot log reports how many were unreachable).
- Sign-up and admin-create now detect an existing address case-insensitively,
  so the same person cannot end up with two accounts.
- A refused sign-in now logs **why** — no such account, wrong password, or a
  stored value that is not a hash at all. The response is still a bare 401, so
  the reason is not exposed to the browser.
- Account creation is logged, so "I signed up and cannot log in" splits into
  "the account was never created" and "it exists but the password fails".

**Admin**
- Users can be edited (email, name, company, phone, role), have their password
  set, and be deleted. Guards: you cannot delete the account you are signed in
  as, and you cannot delete, demote or deactivate the last active admin.
- Ask Ollie now runs on one account-wide API key set by an admin, capped at 20
  answers per user per day (configurable; 0 switches it off without deleting the
  key). A user with their own key uses that and is not capped. Usage per user is
  visible in the admin panel.
- Build versions panel on the dashboard.

**Numbers we were getting wrong**
- "What moves value here" reported **+$1.04M for a bedroom** in Remuera against a
  $1.70M median, and a bathroom worth more than a bedroom. The estimator compared
  two three-sale medians and never controlled for land, so the bedroom was
  standing in for a few hundred square metres of section. Replaced with a
  regression across every sale in the suburb, holding floor area and land area
  constant, published with a 95% interval and withheld when the sales cannot
  separate the room from the house.
- Listings advertised by negotiation or auction carried a hidden search price
  that we were publishing as an asking price, a valuation and a margin. Those
  listings keep their valuation and lose the invented price.
- `_median` returned the upper of the two middle values on an even count,
  biasing every even-count median in the suburb panel upward.
- Subdividable sites were ranked by lot count, putting a four-lot site in a cheap
  suburb above a one-lot site worth six times as much. Now ranked by net gain.

**Screens**
- Suburb pickers are dropdowns everywhere, built from the batch with sold and
  live counts — a free-text box could not tell a typo from a suburb that is
  genuinely not in the data.
- The dashboard shows the top 3 underpriced (by dollar gap) and top 3
  subdividable (by net gain), not just totals and a link.
- The monthly suburb series can be split by bedroom count.
- The assistant is called **Ask Ollie**.
- Geo endpoints degrade instead of 500ing when their tables are missing.
