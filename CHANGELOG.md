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

## v1.36 — 2026-08-30

**The ways a question could still hang, or charge you for nothing**

Found by going looking rather than by them happening to somebody. "It never
times out" is only true if there is no path where the asker waits for ever.

- **A question orphaned by a restart polled for ever.** A worker thread dies
  with its container — a redeploy, an OOM — and nothing is left to finish the
  row, so it said "running" for ever and the browser polled it for ever, the
  counter creeping toward a milestone that would never arrive. That is not
  taking its time; it is a hang wearing patience as a disguise, and it is worse
  than the timeout it replaced because there is no end to it at all. A question
  with no sign of life for fifteen minutes is now released and says what
  happened — roughly triple the longest a single step can legitimately take, so
  one that is genuinely working is never mistaken for a dead one.
- **An unanswered question burned the daily allowance.** The row is written when
  a question is asked now, not when it is answered, so counting rows charged
  people for questions that gave them nothing — including orphaned ones that
  never would. Answers are counted, which is what the rule always claimed.
- **The poll loop outlived the page.** Navigate off Ollie mid-question and it
  kept polling for the life of the tab. It has no attempt ceiling on purpose, so
  nothing ended it.
- **Walking away lost your answer.** The question finishes server-side
  regardless, but the conversation lives in page state, so the answer to a
  question you had walked away from was never shown to you. It is now picked
  back up on the way in.
- **An empty answer rendered as an empty bubble.** A model can come back with no
  text at all; served as an answer that is a blank box, which reads as a broken
  product rather than as a question that did not land.
- Hardening: client-sent history was capped at nothing while the question itself
  was capped at 2000 characters. Truncated per turn rather than refused.

---

## v1.35 — 2026-08-30

**Ollie takes as long as he needs, and counts while he does it**

A question used to be answered inside the request that asked it. That put a
proxy's patience — not the question — in charge of how long an answer was
allowed to take: a hard one was cut off mid-flight and reached the browser as
"500 from /api/assistant: HTTP 500 with no response body", a dropped connection
wearing a crash's clothes. The defence was a 55-second self-imposed deadline —
give up first, and at least say so. An honest answer to the wrong question.

- Asking is a job now. `POST /api/assistant/ask` writes the question down and
  hands back an id in milliseconds; a worker thread answers it with no deadline
  at all; `GET /api/assistant/ask/{id}` reports how far along it is. Nothing
  holds a socket open, so nothing can cut one, and the answer takes exactly as
  long as it takes.
- A counter in the corner of the Ask page, 0-100%, with what he is doing under
  it — thinking it through, looking up the data, writing the answer. The number
  is real: it rises as steps genuinely complete and reaches 100 only once the
  answer is on the screen. Between steps it eases forward on elapsed time, and
  is capped a point short of the next milestone so it can never claim work that
  has not happened.
- A job gets more room to work than a request did — 24 rounds rather than 12 —
  because rounds, not seconds, are the honest bound when nothing is waiting.
- One call still has a ceiling. Unbounded overall does not mean unbounded per
  call: a socket that has silently died must not hang the worker for ever.
- Failures are reported rather than dropped. A question that breaks now finishes
  as "failed" with the reason on it, readable both by the asker and in
  Admin → Ask Ollie key.
- Fixed while here: the deadline was frozen at import. Written as a default
  argument it was evaluated once, when the function was defined, so changing the
  module value afterwards silently did nothing.

The synchronous endpoint is untouched and keeps its budget — it still answers
inside its own request, so it still has a proxy to beat.

---

## v2.6 — 2026-08-16

**The suburb filter — the actual cause, found by using it**
- Choose a district, then choose a suburb that is not in it, and the page
  empties. Both filters are applied and no listing can satisfy both. Nothing on
  screen says the two disagree, so it reads as the suburb filter being broken.
  It was not: it was doing exactly what it was told.
- Choosing a district now narrows the suburb list to that district, so the
  contradictory pair cannot be built. Changing the district also clears a suburb
  chosen under the old one, because it is almost never inside the new one.
- Applied on both the all-properties filter bar and the deal-finder bars.

**A day-boundary bug the tests had been hiding**
- The assistant's daily allowance compared an aware New Zealand midnight against
  a timestamp the database writes in UTC. SQLite compares timestamps as text, so
  for the twelve hours where the New Zealand date is ahead of the UTC one, every
  answer read as belonging to yesterday and the allowance never appeared to be
  spent. The comparison is now made in UTC.
- It surfaced because the suite ran at 08:19 NZ instead of the afternoon. Worth
  saying plainly: this was luck, not diligence.

---

## v2.5 — 2026-08-15

**Trace one suburb through the real data, in one click**
- Type a suburb next to the Diagnostics button on the admin dashboard. It prints
  what the filter resolves that name to, how many listings match, how many are
  in the batch at all, and every stored suburb whose name contains it. Whichever
  number is zero is the fault — no more guessing from outside.

---

## v2.4 — 2026-08-15

**Suburb matching no longer depends on the two feeds agreeing**
- The last shape that fits "select a suburb and nothing happens, for EVERY
  suburb, while district is fine": the picker is built from one feed's
  vocabulary and the filter runs against the other's. A sold archive saying
  "Remuera" against listings saying "Remuera, Auckland" is one suburb written
  two ways, and an exact comparison calls them different places — so every
  option matches nothing. District survives because its vocabulary is small and
  shared.
- A name that finds nothing exactly is now retried against the part before the
  first comma, so a region qualifier on one side and not the other stops
  mattering. It is a fallback only: it can never widen a filter that already
  matched, and "Mount Eden" still does not pull in "Mount Albert".

Together with v2.2 (the picker offering only the live listings' own suburbs)
this covers every cause I can construct for that symptom.

---

## v2.3 — 2026-08-15

**Diagnostics reports what the two feeds call their suburbs**
- One explanation for "pick a suburb, nothing happens" cannot be tested from
  outside: if the sold archive and the live listings spell or scope suburbs
  differently, then every name the merged dropdown offers can be a name no live
  listing carries — so nothing matches, for any suburb. District keeps working
  because it has a small shared vocabulary.
- The Diagnostics button now prints both vocabularies with a sample of each and
  how many names appear in both. `in_both: 0` is that fault, visible at a glance.
- v2.2 already makes this impossible on the properties page by building the
  picker from the live listings alone — every option is then a name the page can
  actually return.

---

## v2.2 — 2026-08-15

**Why trends worked and all-properties did not**
- The suburb list merged the SOLD archive with the LIVE listings. The archive
  covers far more suburbs than any single week of listings, so the properties
  page was offering suburbs with nothing live in them. Pick one, get a blank
  screen — indistinguishable from a filter that does nothing.
- Trends worked because it reads sold data: every option it offered had sales
  behind it.
- `/api/properties/suburbs` takes `dataset=for_sale|sold|any`. The properties
  filter asks for `for_sale`, so every option it offers has listings behind it;
  the trends picker keeps the full list, because a suburb with no live listings
  still has years of sales to chart.

---

## v2.1 — 2026-08-15

**One rule for matching a suburb, everywhere**
- Suburb trends worked while the properties filter did not, and that difference
  was the whole bug: trends matched with `ilike` (case-insensitive), the
  properties filter with `==` (not). So the real mismatch in the data is CASE,
  and only the endpoint using the stricter rule broke.
- Every endpoint that takes a caller-supplied suburb or district now resolves it
  the same way — the sold list, the per-suburb sale-method breakdown behind
  "best way to sell here", and the trends panel itself, which was tolerant of
  case but not of stray whitespace.
- The remaining `==` comparisons match a suburb read off the property row
  itself, so they agree by construction and are left alone.

---

## v2.0 — 2026-08-15

**Choosing a suburb did nothing**
- On the all-properties page, picking a district zoomed the map and picking a
  suburb didn't. The suburb dropdown is built from TRIMMED names — it has to be,
  or the same suburb appears three times — but the filter compared the column
  exactly, and the scraped values are not clean. `"Remuera"` never equals
  `"Remuera "`, so it matched nothing. With no points the map has nothing to fit,
  so it stayed put, which looks identical to a control that does nothing. The
  LIST was equally broken; it was just less obvious.
- District only kept working by luck: its options are hard-coded to the raw
  stored strings.
- Suburb and district names now resolve against the spellings actually present
  before filtering, so every option matches the rows it was built from. The
  comparison stays an indexed equality rather than wrapping the column in
  trim()/lower().
- The dropdown groups spellings case-insensitively and shows the one that
  appears on the most listings, with the counts summed across all of them.
- The map states an empty result instead of sitting still. Not moving is how
  this went unnoticed in the first place.

---

## v1.9 — 2026-08-15

**The cause behind three separate "bugs"** — found in the production logs
- The boot log has no trace of `db_bootstrap`, so the start command Railway is
  using is not the Procfile's. It has never run. Every table added after the
  original schema was therefore missing — `assistant_logs`, `app_settings`, the
  geo tables, `bug_reports` — and the geo 500s, the assistant 500s and the empty
  assistant usage table were each diagnosed as their own separate fault.
- The app now creates any missing table itself on startup and logs which ones,
  so the schema depends on the application starting rather than on a start
  command nobody can see.

**Bug log**
- `bug_reports` created by v1.6 lacks the four columns v1.7 added, and
  `create_all` never alters an existing table — so every query failed with
  "no such column: bug_reports.source", making the bug log the one screen that
  could not report its own fault. Missing columns are now added in place, with
  existing rows backfilled and kept.
- A single row with a null timestamp used to fail validation and take the whole
  list down. One odd row now costs that row's timestamp and nothing else.

**Today's brief**
- The top 3 underpriced and top 3 subdividable are admin only, withheld by the
  API rather than merely hidden in the page. They name the specific houses with
  the biggest margins in the batch, and a field the browser is trusted not to
  render is a field anyone can read.

---\n\n## v1.8 — 2026-08-15

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
