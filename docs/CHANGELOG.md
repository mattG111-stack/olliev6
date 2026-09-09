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

## v1.58 — 2026-09-08

**A clean full build**

Same code as 1.57 — every fix from 1.50 onwards — renumbered and re-cut as
complete repositories after the GitHub repositories were emptied. Nothing in
these zips is a partial upload: each set together is the whole repository, and
that was verified by extracting the zips into an empty directory and building
and testing from THOSE, not from the working copy they were cut from.

---

## v1.57 — 2026-09-08

**Two live faults found by running the tests against Postgres for the first time**

The suite has always run on SQLite. Production is Postgres. They disagree on
exactly the things that have already cost a deploy, and every disagreement has
the same shape: SQLite is permissive so the test passes, Postgres is strict so
the live insert fails. A green suite was evidence of nothing.

Both of these were already broken on the live site:

- **`price_flag` was VARCHAR(48) and its message is 54 characters** — *"10% of
  the Flat Bush median — check for a missing digit"*. SQLite ignores a column's
  length; Postgres refuses the row. So the flag that exists to catch a price
  with a digit missing or added made the listing it was about impossible to
  write — the one record most needing a human eye was the one that fell out of
  the run. (`best_strategy` at 32 was the same fault in v1.39. Twice is a
  pattern.) Widened to 200; the boot-time check grows the column on deploy.
- **The run-log workbook has answered 500 every time it was ever pressed.** It
  wrote a timezone-aware timestamp into a cell and openpyxl refuses one.
  SQLite does not store timezones, so the tests never reached the line. Times
  are now converted to New Zealand time before the zone is dropped — simply
  discarding it would have printed every time in the workbook half a day out.

**Nine new tests, and they do not name those two.** Naming them catches what is
already found. They go looking instead: every generated message is measured
against the column that has to hold it, and every module that writes a
spreadsheet is checked for timezone handling. Five of the nine fail on v1.56,
and they run on SQLite — so this class of fault is caught from now on without
needing a Postgres run at all.

**Verified on real Postgres:** 1,763 passed, 0 failed. And the rule, end to end
at volume:

```
big file of 2,000                ->  2,000 live
short file of 150 on top         ->  2,150 live   nothing dropped
50 of the same houses re-listed  ->  2,150 live   no double-ups
30 sensed sold, 20 new arrive    ->  2,140 live   only the 30 left
duplicates 0 · sensed-sold still showing 0
```

---

## v1.56 — 2026-09-08

**"Archived" was the wrong word and it cost a day**

    "no fuck it they should all be live ... only archived once you find the
     property is sold otherwise they just keep loading in the system till it
     triggers its sold"

That is the rule, and it is the rule the system is built on. Read against a
9,281-row load sitting under a 672-row one, the word "archived" says those 9,281
houses were archived — which is exactly what anybody would conclude, and what
the v1.51 fault had actually made true.

A superseded **file** is not superseded **listings**. Everything in it that is
still advertised is carried into the current load, and a listing leaves only
when the daily link check opens its advertisement and finds it gone.

The import history now says **superseded**, with a line underneath saying what
happened to the houses: *its still-advertised listings were carried into the
current load*.

**Proof, at your own numbers scaled 1:10:**

```
after the big file            928 listings live
after a 67-row file           995 listings live   nothing dropped
after 5 confirmed gone      1,000 listings live   only the 5 dead ones left
```

---

## v1.55 — 2026-09-08

**Three things were public that should not have been**

    "nothing should be public"

None of them were a hole somebody opened. All three were a default nobody
turned off.

- **The whole API was documented to anyone who asked.** FastAPI serves `/docs`,
  `/redoc` and `/openapi.json` unless you tell it not to, and nothing had. The
  specification was 263 KB describing all **151 endpoints** — every admin route,
  every parameter, every field name — to anybody who typed the address. Off
  now, and on only where somebody sets `API_DOCS=1` deliberately.
- **The self-test printed the workings.** `/selftest` was open and showed two
  real properties by name, their council valuations, what we value them at, and
  a paragraph explaining how the pricing guard decides. Administrators only now.
  `/api/version` stays open — it answers which build is running and nothing
  else, which is what you need when nobody can sign in.
- **The readiness probe named the database.** 846 bytes listing table names,
  quoting the SQL, and saying what was currently broken. It has to stay open —
  the platform's health probe cannot sign in — so it now returns the verdict
  per check and no detail. The reasons moved to `/api/admin/ready`, one sign-in
  away rather than nowhere.

**The test that matters here is not about those three.** It walks *every* route
the application has, works out which carry no authentication, and fails on
anything not on a short list where each entry has a written reason for being
open — the front door, the payment provider's signed callback, a referral link,
the image proxy, the platform's health probe. A test listing known-bad routes
only ever catches the leaks somebody already found. This one catches the next
one, on the day it is added.

Nine of its ten checks fail on v1.54.

**Still to do at your end, and I cannot do it for you: both GitHub repositories
are public.** I fetched them just now with no credentials and got the code.

---

## v1.54 — 2026-09-08

**A tidier repository — no code moved out of the way, none deleted**

    "inside the files cant you pack some of the files in one folder so when i
     have to delete stuff out of github its easier but do not delete any code
     just pack it better"

The backend front page on GitHub had **twenty loose files** sitting above the
folders. It now has nine, and every one of those nine has to be there — Railway
reads `requirements.txt`, `Procfile`, `runtime.txt`, `railpack.json` and
`nixpacks.toml` to work out how to build and start the service, pytest reads
`pytest.ini`, alembic reads `alembic.ini`, and `README.md` and `.gitignore` are
what they are. Move any of those and the deploy breaks.

Everything else went where it belonged:

- four documents → `docs/`
- seven one-off development scripts → `scripts/`, which already held fifteen

The frontend went from eleven loose files to nine the same way. Its remaining
nine are all required at the root by Next.js, npm, TypeScript, Tailwind and
Playwright.

**Nothing was deleted and no application code moved.** Git records all eleven as
renames, most of them byte-for-byte identical. The five moved scripts that
import from `app` carry the same three-line path fix every other script in
`scripts/` already had — without it they would have failed on `import app` the
moment they were no longer sitting at the root.

**Two of those scripts were already broken before the move**, on their own stale
test stubs, and still are. They are superseded by the real test suite in
`tests/` and were left exactly as they were rather than quietly rewritten.

---

## v1.53 — 2026-09-08

**Download the live data with the checks already run**

    "also have an exel download for all live data so i can download it and you
     audit it for bugs"

A new button on the properties page: **Live data + checks (Excel)**.

There was already a CSV export, and a CSV of nine thousand rows is not an audit
— it is nine thousand rows. Every fault this system has shipped was visible in
the data at the time and nobody could see it, because seeing it meant knowing
which column to look down and what the number should have been:

- 1,436 of 1,586 listings "advertising" a price, in a market where four houses
  in five sell by auction or negotiation and name none
- a headline deal of +2,296% against a council record that had valued the land
  and not the house
- 672 listings live where 9,281 had been the week before

So the workbook leads with a **Checks** sheet: each row a question, the answer
beside it, and a plain statement of what the answer should look like. Anything
off is highlighted. You do not need to know how the system works to see that
something is wrong.

The second sheet is every row, so anything the checks did not think of can still
be found by hand.

**It includes the listings a customer CANNOT see** — held back at review,
advertisement gone, a price the scraper invented — with a column saying whether
each row is visible and one saying why not. Those rows are exactly where a fault
hides, because the site by definition never shows them. Filter on that column in
Excel for either set.

No new dependency: openpyxl was already here for reading uploads.

---

## v1.52 — 2026-09-08

**Put the missing listings back — no re-upload**

    "im not reup loading the data it too muck work if the data is still there
     just make it show"

There is now a button. Where an earlier upload was smaller than the book, the
publish screen says so in plain numbers and offers to put them back. The
listings return exactly as they were — price, photographs, subdivision, holds —
because they were priced when they loaded and are still sitting there. Press
Re-price afterwards if you want them against this week's comps. Safe to press
twice; the second press finds nothing to do.

**A correction.** Archiving a batch only hides it, which is why this works. But
"nothing is ever deleted" was wrong: retention keeps the newest twelve batches
per type and region and DELETES the rest, rows and all. An archived book
survives about a dozen more uploads, not for ever. That is why this is a button
on the screen rather than a note in a file.

**And a landmine found while checking that.** Retention kept the newest twelve
batches BY UPLOAD DATE — and "newest" is not "live". A batch goes live when
somebody presses Publish, and staged batches keep arriving in the meantime, so
twelve uploads reviewed and not published would have pushed the batch the site
is actually serving out of the window and deleted it. Every listing on the site,
gone for good, from a routine tidy-up at the end of an upload that changed
nothing a customer could see. The live batch is now pinned whatever its age.

**The weekly link check, which you asked about.** It runs daily, not weekly, and
it hides rather than deletes — reversibly, so a link that answers again puts the
listing straight back. Only a 404 or 410 counts as evidence; a timeout or a rate
limit means we could not look, which is not the same thing. Two faults were
stopping it getting round the book, and both mattered more than they used to,
because since v1.51 this sweep is the only thing that takes a listing off the
site:

- **It was checking every batch ever loaded**, not just the live one — so with a
  dozen batches on file, roughly one check in twelve landed on a listing anybody
  could see. The daily budget now goes entirely to the live book.
- **The weekly carry did not bring a listing's check history with it**, so after
  every upload the whole book looked never-checked. The sweep works
  oldest-checked-first, so it restarted at the top each week and never reached
  the bottom. Neither fault showed as an error: the sweep ran, reported hundreds
  of checks, and was quietly re-checking the same front slice for ever.

---

## v1.51 — 2026-09-08

**A short upload no longer hides the book**

    "so i just added the lastest data and its hidden the old data instead of
     just adding in the new ?"

    Older batch  #55 · 6/09/2026 · 9,281 rows
    Newer batch  #56 · 8/09/2026 ·   672 rows

Carrying the book forward was built in v1.35 and it worked. What broke it was
the **audit screen**: once an upload staged for review instead of going live
immediately, every real upload started arriving as "not published yet" — and
the carry was written to run only when a batch went straight live. It became
dead code on the only path anybody uses.

Nothing looked wrong at any point. The upload succeeded. The review screen
showed exactly the rows the file contained, which is what a review screen is
supposed to do. The loss happened later, at Publish, in a different part of the
system, when the batch holding the other 8,609 listings was archived.

A 672-row file arriving over a 9,281-row book is a scrape that caught less this
time, not a market that emptied overnight. A listing leaves the site when its
**advertisement** is gone — the daily link check decides that, by opening it —
and never because the newest spreadsheet was short.

- The carry now runs whether or not the batch goes straight live, so **what you
  audit is what goes live**. The review screen shows the whole book.
- Archiving still waits for Publish. The site keeps serving what it was serving
  until somebody approves the new load.
- A listing in both files is not duplicated — the newer file wins.
- A delisted house still leaves. This is not "keep everything for ever".

**Why the tests did not catch it.** There is a whole file of tests proving the
carry works, and every one of them calls the carry directly. Not one went
through an upload. The rule was right; the caller was not covered. The new
tests upload a file the way the app uploads a file, press Publish the way an
operator presses Publish, and count what a customer can see.

**If your book is short right now:** the missing listings are not gone — they
are still in the database under the older batch. Re-upload the earlier, larger
file once on this build and they come back, with the newer listings carried
along with them.

---

## v1.50 — 2026-09-07

**Nothing the browser receives says where the data comes from**

    "as I can click on that I already knew what is where the photos were from
     etc all from embedded links you can also walk your full dataset"

It did not even take a click. Three leaks, all readable in the network tab on
page load, all fixed the same way: the address never leaves the server.

- **Photographs.** Every one loaded straight from the portal that supplied it,
  and `image_urls` handed over the whole gallery path per listing in one field.
  The source URL is now *encrypted* into a token only this server can read, and
  the browser is given a path on our own domain. Base64 would not have done — an
  encoding anybody can reverse is not a protection.
- **The listing link.** The supplier's address, handed over on purpose, because
  it is the thing a customer clicks. It is a redirect we own now: the customer
  still lands on the real advertisement, and the address only resolves at the
  moment they go.
- **The field names.** Every row carried keys reading `oneroof_valuation`,
  `oneroof_url`, `tm_valuation` — a list of who we buy from, on every row, to
  every logged-in customer. The number stays and the name goes.

Deleting those fields was the wrong fix, and a test said so: an estimate we pay
for, store and never publish is a lookup we wasted. The database columns are
unchanged; only the wire key is neutral.

**Photographs are still full size.** The frontend asked the CDN for a bigger
copy by rewriting the URL, and it can no longer see the URL to do it — so it
asks and the proxy applies it to the real address. Without both halves every
photograph on the site drops back to the ~300px thumbnail the scrape stored.

**The proxy is not an open fetcher.** It will only fetch hosts our own listings
already point at, and with nothing to go on it refuses everything rather than
allowing everything — the opposite would turn it into a way to make our own
server reach whatever the container can reach.

**No supplier is named in the code either.** A hard-coded allow-list is a list
of who we buy from, written down in the repository, which defeats the point.
The list is worked out from the data; a test reads the proxy's source and fails
if a supplier appears in it.

**Admin screens still name the sources.** The operator is entitled to know, and
approving a fill without knowing where it came from is not a decision. One
label on that screen was wrong and is fixed: OneRoof writes to its own column
now, so the feed's own figure is no longer captioned as OneRoof's.

---

## v1.41 — 2026-08-30

**You can type in Ollie's box again**

Reported as "you can't write in the search bar of Ollie, it's locked". Two
causes, both real.

- **The orb was taking the clicks.** Ollie's name and the question box are
  pulled up *into* his canvas by negative margins — that is how the layout
  closes the empty space he leaves around himself when idle — so on a desktop
  70px of a 640px canvas sits directly over the box. A decorative canvas that
  accepts clicks there IS a box you cannot click, while the suggestion chips
  below it still work. It no longer accepts them, which is what it should always
  have done.
- **A key that could not be read was reported as no key at all.** A key
  encrypted under a different secret than the one now running — exactly what a
  redeploy with a fresh `JWT_SECRET` produces — is intact but unreadable. The
  box greyed out and said "Add an API key in Settings first": an instruction to
  go and buy something, shown to the one person who cannot fix it. Ollie now
  says the key is saved but unreadable, that an administrator re-enters it, and
  that there is nothing to do at your end.

**If the box is still locked after this build**, the second cause is the likely
one: check whether `JWT_SECRET` changed on the last deploy, and re-enter the
assistant key in the admin panel.

Verified by typing a real question at 1440px and 390px, with a working key, on
first run, and with the key both missing and unreadable.

---

## v1.40 — 2026-08-30

**Show what the winning plan beat**

1.39 worked out both subdivision strategies and then kept the comparison to
itself — the endpoint returned only the winner, so the page had no way to show
what the headline had beaten. Half of displaying a choice is showing the choice.

- The subdivision panel now reads **"Keeping the house instead: $366,964 —
  $50,956 behind"** under the net gain, and the same the other way round where
  retaining wins. Knowing that keeping the house is $51k behind is as useful as
  knowing that knocking it down is $51k ahead.
- The demolition allowance is editable in the calculator with everything else.
  It is the figure that decides how often demolition wins, so it belongs where
  the arguing happens.
- Shown only when both figures exist. Where the retain profit cannot be computed
  there is no comparison to draw, and inventing one would be the same mistake
  the engine was just taught not to make.

---

## v1.39 — 2026-08-30

**Demolish leads when demolishing earns more**

Two ways to develop a site with a house on it: keep it on one lot and sell the
surplus, or knock it down and sell every metre. Only the first was ever costed
unless a developer went looking for the other, so the headline was always
"retain" — on a site where a tired house sits on land worth far more without it,
that is the wrong answer given confidently. Both are costed now, the bigger
number leads, and the one not taken is shown beside it so you can see what was
given up. A tie does not demolish: it is irreversible.

**Two things had to be fixed first, and both were bigger than the feature.**

- **Demolition could not win, on any site.** The $100k allowance for doing up a
  *retained* house was also being charged as the cost of *knocking one down*.
  Because the refurb comes off inside the retain figure and the demolition comes
  off outside it, retaining came out ahead by construction — a 168-case sweep
  found demolition winning exactly zero times. Knocking a house down is not the
  same job as doing one up, so it now has its own editable figure, **$35,000**.
  With the two held apart, a derelict house on 1,500 m² bought at $600k earns
  $417,920 demolished against $366,964 kept. Demolition still wins rarely — 5
  cases in 168 — and only where the house adds nothing.
- **`best_strategy` was a 32-character column.** "Retain house + sell new
  sections" is exactly 32; the demolish wording is 39. Postgres refuses the
  write and SQLite ignores the limit, so this would have passed every test and
  killed every pricing run once live. Widened, and the boot-time schema check
  can now grow any text column the app outgrew rather than only add new ones.

**And one caught on the way:** demolition's profit does not depend on what the
house is worth, so it is computable on a site where the retain figure is not.
Leading with it there would have recommended demolishing a house nobody had
valued — precisely because nobody had valued it. Where the retain figure is
unknown the answer stays unknown, and the demolition number is shown as
information rather than advice.

**Note for anyone tracking a figure:** sites where demolition now wins will show
a different plan and a different profit than they did before this build.

---

## v1.38 — 2026-08-30

**Ollie on a desktop: ask on the left, answers on the right**

- **You could not ask the first question.** The hunt questions were rendered
  *instead of* the whole page, so a customer opening Ollie for the first time met
  a form — no orb, no question box, nothing to ask with — until they had answered
  it or found the skip link. The questions are worth asking; they are not worth
  being the only thing on the page. They are content on the answers side now, and
  the box is always there.
- **Two columns above 1000px.** Everything you *do* sits together under Ollie —
  his name, the box, what he is watching — and everything he *gives back* is on
  the right, newest at the top, read downwards. The left column is sticky, so the
  box stays with you however far down the answers go: you never scroll to ask a
  second question, and neither half moves when the other fills up.
- **The orb is 40% bigger**, 457 to 640, and his column is sized to him rather
  than to a percentage. The reading column keeps a 420px floor, so on a smaller
  desktop *he* gives way rather than the answers being squeezed into a gutter.
- The 0-100% counter moves under him on desktop. It is his state, not the page's,
  and beside him it reads as one object thinking rather than a download.
- The suggestion chips move to the answers side, standing where the answers will
  appear — so that column is never a blank half-page before the first question.

Below 1000px everything reverts to the stacked layout, which already worked.
Phones are untouched.

---

## v1.37 — 2026-08-30

**The maps came back blank. Two causes, both mine.**

- **A chosen source was assumed to be a working one.** The street map was picked
  once from config and trusted. The LINZ key was not answering, so every tile was
  refused and the panel was an empty grey rectangle with a confident LINZ credit
  under it — which reads as a map still loading, for ever. Choosing a source is
  not the same as it working, and the only place that difference is visible is
  the browser fetching the tiles. Config now gives an ORDER, and the first source
  that actually draws is the one that stays. A wrong key costs a plainer map
  instead of no map.
- **Leaflet itself was fetched from a CDN at runtime.** The location panel added
  a script tag pointing at unpkg on first render, while `leaflet` sat in
  package.json already installed and already built. Every customer's map depended
  on a third party answering. The app's other two maps import it from the bundle;
  only this one did not — the kind of drift that survives because the odd one out
  works most days.
- If every source fails, the panel says so rather than showing grey. The location
  circle and caption are ours, not the tile server's, so they still draw.

Proved in a browser with LINZ forced to refuse every tile: the map falls through
to Esri and re-credits it, and with both sources down it says the street map
could not be loaded.

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
