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

## v1.84 — 2026-09-17

**Nothing had ever opened the app on a desktop**

    "run moblie tests and pc"  /  "and tablet"

Phone, small phone, tablet and tablet landscape were all covered. The widest
viewport in the responsive suite was 1180px — narrower than a laptop. So until
this build nothing in the suite had ever opened the app at the width most
customers actually use it at, and "the tablet tests pass" had been standing in
for it.

That is not the same question. The faults at 1280 and 1440 are different ones: a
fixed max-width that leaves half the screen empty, a table that only begins to
scroll sideways once there is room to show every column, a panel that grows past
its container. None of those can appear on a phone.

Added laptop 1280 and desktop 1440. Both pass, which is the answer — but it is
now an answer rather than an assumption.

Full run at every width: 137 responsive and mobile-audit, 88 functional, 2,015
backend, typecheck and production build clean.

---

## v1.83 — 2026-09-17

**The browser tests have not run since v1.54**

    "run a full set of tests"

Worth doing, because the first thing a full run found was that a third of it
had not been running.

**Twenty-eight builds with no browser coverage.** In v1.54 the repository root
was tidied and `seed_e2e.py` moved into `scripts/`. The Playwright config still
looked for it in the root, so the web server never started and not one of the
185 browser tests ever ran. The failure is silent in the worst way: Playwright
reports a config error, and a config error reads as the harness being awkward
rather than as no coverage at all. Exactly what that file's own comment about
the Chromium path warns against, arriving by a different route.

With the path fixed, 184 of the 185 passed. The one that did not was real.

**/trends did not fit a 360px phone.** Three stat tiles go to two across on a
phone, which was fixed once at 390px and assumed to hold all the way down. At
360px — a very common phone — two across is 126px a tile, and the label wraps
until the tile is 125px TALL: a square of stacked words with a number somewhere
in it. One per row below 400px.

**And a flaky backend test was a test isolation fault, not a flake.** The fill
endpoint answers immediately and works on a daemon thread. That thread outlived
its test, so monkeypatch put the real lookup back while it was still going — it
then called out to the internet and wrote into the database the NEXT test had
just rebuilt. The test that failed was whichever one the stray writes landed in,
which is why it moved between runs and passed whenever the file was run alone.

The fixture that fixes it has to depend on `monkeypatch` to work at all:
fixtures tear down in reverse order of setup, so an autouse fixture is torn down
LAST — after the real lookup is back — and joining then is joining the thread
you were trying to stop.

Full run after all three: 2,015 backend, 185 browser, typecheck and production
build clean.

---

## v1.82 — 2026-09-17

**Underground services is off**

    "then dont have the feature for now"

Right call. It is written, it is tested, and nobody has ever seen it draw a real
pipe. Every one of those tests runs against a stub written from an understanding
of how these services behave rather than from one that has answered, and the
difference between those two things is the whole feature.

The specific risk is not that it breaks loudly. It is that it works and says
nothing: the loader keeps only pipes whose owner reads STORMWATER, WASTEWATER,
WATER or WATERCARE — names taken from an Auckland COUNCIL stormwater export. If
Watercare spells it differently, every pipe is dropped, the request succeeds,
and the panel says "no services near this property". A developer reads that as a
fact about the ground.

So the button is gone from the property page. Nothing is deleted: the endpoint,
the fetcher, the licence handling and the two thousand tests all stay. Turning it
back on is one constant in SunMap.tsx plus the three service URLs on the backend
— after somebody has run `probe` against the real service and we have looked at
what it actually says.

---

## v1.81 — 2026-09-17

**Drawing a suburb, not a diagram**

    "can you now draw the pipes on the maps right ?"

Not quite, and reading the drawing code rather than trusting it turned up three
things that would each have made a real suburb look like a mess.

**A bore label on every pipe.** Fine on the six-pipe examples we had been
building, unreadable on a street: a few hundred runs in view, every label
overlapping two others and hiding the streets the pipes are meant to be read
against, and a few hundred DOM nodes rebuilt on every pan. Labels now appear only
close in, capped, and spent nearest-first — so when the budget runs out it goes
on the runs beside the property rather than on whatever the service happened to
return first.

**Manholes at a fixed size whatever the zoom.** A manhole is a one metre
chamber. At a fixed nine pixels it is a dot on one house and a solid mass over a
suburb, where a street's worth of them merge into one blob.

**And nothing redrew on zoom.** Both of the above are zoom decisions, so without
it you zoom in expecting labels and nothing changes until you happen to pan.

**The fourth was not cosmetic.** A dense view can exceed what the operator's
service will return at once. It says so — and we were drawing the fraction it
gave us without passing that on. A map with streets missing their mains, and
nothing on it to tell "no pipe here" apart from "we did not ask for enough".
That is the silent truncation guarded in the bulk fetch since v1.75, arriving
through the live door. The panel now says it is showing part of the network.

---

## v1.80 — 2026-09-17

**`autowire` — the layers find themselves**

    "One roof has that view how can't we"

We can, and what stood in the way was configuration. Two specific things in it,
and the screenshot showed both.

**Their legend has three entries: water, wastewater, and wastewater MANHOLE.**
The mains and the nodes are different layers on these services. We allowed one
URL per kind, so the manholes were never going to appear — and a manhole is the
half of the drawing that matters most, because it is where a wastewater
connection actually gets made. Each setting now takes several layers, separated
by commas, and one of them failing is one layer missing rather than a kind
missing or a page failing.

**And the layer numbers were left to a person.** Reading sixty names off a
service listing and picking numbers by hand is how you get a number that is
silently wrong the day the operator renumbers their service: the layer returns
nothing, the map shows a suburb with no water, and nothing anywhere says which
of those two happened.

    python scripts/load_geo.py autowire "<service-url>"

finds the water, wastewater and stormwater layers by name, checks each one
answers, and prints the settings to paste into Railway. Three rules in it are
worth stating because each is a wrong drawing avoided. "Stormwater" contains
"water", so it is tested first — otherwise every stormwater layer is drawn blue
and reported as the distance to a water connection. A catchment or a supply zone
is a polygon covering half the district, not a pipe; drawn as a main it would put
a connection across every property in it. And a hydrant or a valve is a fitting,
not a connection: offered, it would sit outside almost every house and become
"the nearest water", which is wrong in the direction that costs money.

Nothing to load afterwards. The property pages ask the service directly.

---

## v1.79 — 2026-09-17

**`load_geo.py sample` — one suburb's pipes, out to a file**

    "show me a map was riverhead and how the pipes go ?"

Not from here: every host this would need — Watercare's own server, their open
data portal, the ArcGIS feature services and LINZ — is blocked outbound from the
build sandbox. And a pipe map of a real suburb is not something to approximate.
An invented boundary on an invented street is an illustration; invented mains
under Riverhead is a drawing somebody could dig against.

So instead, the command that closes the gap:

    python scripts/load_geo.py sample "<layer-url>" riverhead 800 out.geojson

It pulls the mains around one place straight out of the service and writes what
came back, unchanged. Places can be given by name or as lat,lng. It reports
whether the answer was complete, and refuses to pretend an empty one is a
finding about the ground rather than a wrong layer number.

Two uses. Checking a service's field names against a real extract rather than a
guess, before loading anything. And getting an actual suburb into a drawing.

---

## v1.78 — 2026-09-17

**The mains follow the map**

    "It should look like that"  — the network running across every street in
    view, on the basemap, the way the operators' own viewers draw it.

Two things stood between us and that picture, and only the first was obvious.

**The panel asked for a fixed 250 m circle around the pin, once.** A circle ends
mid-road: the main running past the house stops dead a few doors down, and that
gap does not read as the edge of what we asked for — it reads as missing data,
on a drawing whose entire job is to say where the pipes are. Panning or zooming
changed nothing, because nothing asked again.

The view's own bounds are now the query, and moving the map asks again, debounced
so a drag is one request rather than one per frame. A view wider than the default
is not narrowed back to it, which would have left a bounds parameter that did
nothing. Zooming out to the country does not become a request for the country:
past a sane width the window stays around what is on screen — clamped rather than
refused, because a developer who zooms out wants context and an error is not
context. And the distances are still measured from the property: panning changes
what is DRAWN, never what "the nearest main" means.

**And the pipes in view were being chosen by their vertices.** A main is one long
polyline down a street with its vertices wherever the surveyor put them — at the
corners, often hundreds of metres off. Asking whether a VERTEX falls in the
window drops every pipe that merely PASSES THROUGH it, which is all the ones
that matter: the main outside the house vanishes the moment you zoom in on the
house, and the map shows a street with no water in it.

It was latent under the fixed radius too, and it hid behind every example we had
built, because a short invented pipe has its ends inside the window. The test now
runs a main whose ends are 5 km away past a property zoomed right in.

That is the same mistake as measuring to the ends of a pipe, in the other half of
the same panel, found two builds apart.

---

## v1.77 — 2026-09-17

**Two bugs found by drawing a picture**

    "yes but show me a property and what it looks like"

Neither of these came from a test. Both came from rendering one section and
looking at what appeared, which is the argument for drawing things.

**The yield depended on which way round the boundary was stored.** A 609 m²
section took three terraces one way up and NONE mirrored north for south —
identical shape, identical area, identical side lengths, identical everything a
person would think to check.

The packer rotates the parcel so the candidate street edge lies along the x
axis, then anchors its rows at the bottom of the bounding box. Where the
interior lands after that rotation — above the axis or below it — decides
whether the rows start at the STREET or at the REAR boundary. And which side
the interior is on IS the winding order of the ring. A boundary can be
digitised either way round and both are valid, so the answer turned on a
property of the record rather than of the land. Only one row fits in 33 m of
depth: anchored at the rear it lands on the tapered back of the site instead of
the wide street end.

Both results read as findings about the shape. A developer would have believed
whichever one they were shown. The ring is now normalised before packing, and
eight tests hold it there — including one asserting all four descriptions of
the same land agree, and one asserting they do not agree on zero, because a
packer that returns nothing for everything is consistent and useless.

**And the property page measured to the ends of a pipe rather than to the
pipe.** A main runs the length of a street and is nowhere near either end of
itself. A stormwater main 8 m across the road was reported as 107 m away,
because its nearest END was 107 m east. The search module has always measured
to the segment; this panel measured to the vertices, so the two printed
different numbers about the same pipe.

---

## v1.76 — 2026-09-17

**A property asks the service for its own pipes**

    "what are you on about the point of the api is it shows the water and
     waste water pipes"

Fair, and the first cut had the wrong shape. v1.75 built a bulk download: pull a
region's whole network into our database, then measure distances against it. That
is the right shape when the source is a file somebody exports. It is a detour
when the source is a live service, and it meant a house could not show its mains
until somebody ran an import.

A property page has a point and a radius. The service answers exactly that
question in one request. So it now does: nothing has to be loaded first, the
answer is current rather than as current as the last import, and the reply is a
few dozen pipes instead of a hundred thousand — so the paging, the feature cap
and the completeness check simply do not arise.

Where a network HAS been loaded it still wins: that is the whole region rather
than one box, and it costs no request.

**What the tests are mostly written against.** Every way an envelope query goes
wrong returns an empty answer with no error on it, and an empty answer reads on
screen as "no services near this property" — a servicing finding about the house
rather than a fault in our query. Send the box without `inSR` and the server
reads -36.9 as NZTM metres, which is a box in the Southern Ocean. Forget that a
degree of longitude is 89 km at Auckland rather than 111 and the box is a fifth
too narrow east-west, so a main across the road is missed. Neither raises.

**One rule, two callers.** Reading a feature into a main — the field names the
operators use, the retired pipes, the road and park drainage that is not a
connection — is now written once and used by both paths. Separately, the live
path would have excluded a retired main from the distance search and drawn it on
the map: the picture contradicting the number printed beside it.

Each service is configured per kind, so a layer can be pointed at, moved or
switched off without a deploy:

    SERVICE_URL_WATER, SERVICE_URL_WASTEWATER, SERVICE_URL_STORMWATER

A kind with no URL is simply absent, and an operator's service being slow or
down costs that layer rather than the page.

---

## v1.75 — 2026-09-17

**Sold houses come off the site, and the water mains can come on**

    "ive just noticed alot of houses have sold in the last few weeks and we
     still have them on the site shouldnt they be gone ?"

They should have. The link check was never going to catch them, and the reason
is worth stating because it looks like it should work. That check asks whether
the advertisement is still up and treats only 404 and 410 as evidence — which is
the right rule for what it does. But a New Zealand portal does not delete a page
when a house sells. It leaves it up with a SOLD banner across it, answering 200
for ever. The one signal we watched for is the one signal a sold house never
sends, so the listing sat there until somebody replaced the whole batch.

Meanwhile the sale had been arriving in the weekly sold file the whole time,
with an address, a price and a date on it. Direct evidence rather than an
inference from silence, and nothing was cross-matching the two.

**The trap, and the reason this needed care rather than a join.** The sold file
is a HISTORY. It holds the 1998 sale, the 2007 sale and the 2019 sale of the
same house. Match on address alone and every listing whose property has ever
changed hands is retired — which is nearly all of them. The site empties
overnight and nothing errors. So the sale has to be NEWER than the listing: a
house advertised since March with a sale dated April has sold; a house first
advertised last week with a sale dated 2019 has not. A 30-day grace either side
of first-seen covers the commonest case of all, sold days after we indexed it,
where the file carries the agreement date rather than settlement. Listings
loaded before we recorded a first-seen date fall back to a four-month window,
because those are the ones most likely to be stale and skipping them would miss
the problem entirely.

It marks, it never deletes, and it records which sale it matched on, so an
operator can check a hidden listing rather than take it on trust. It runs daily.

**Water and wastewater mains.**

    "all we are after is the water and waste water pipes on the map"

The map, the loaders and the distance measuring already handled all three
services. The missing piece was only ever the data — and reading it exposed two
faults in the fetcher that a FeatureServer had never shown us.

Offset paging is an OPTIONAL capability. A layer advertises it, and a utility's
own GIS, which is usually an older MapServer, does not have it. A server without
it does not reject the offset — it ignores it, and cheerfully answers page one
again. Paging like that returns the same thousand pipes over and over until the
feature cap, and the cap is the only reason it ever stops. The capability is now
asked for, and where it is missing the paging walks the object id instead, which
needs no capability and cannot silently stand still.

And completeness is now asked for rather than inferred. "The last page came back
short, so that was all of it" is precisely the inference a truncating server
satisfies. The layer will say how many rows match, in one cheap request, so the
count is checked against what arrived and a shortfall is named instead of a
fraction of the city being loaded as a network.

`load_geo.py probe` lists what a service holds and what one layer holds, because
the number on the end of a layer URL is not guessable — "water mains" is layer 9
on one service and 21 on the next.

**The credit and the warning.** The mains are published under CC BY 4.0, which
permits commercial use on the condition that the operator is credited, the
licence is linked, and changes are declared. We do change the data: reprojected,
filtered to the public network, and measured against every parcel. The credit is
built from the pipes actually on screen, so a stormwater-only view does not
credit the water operator for it.

And the part that is not a licence condition but a liability: these positions
are indicative. A developer who reads this screen as a service locate and puts a
digger through a water main was misled by us. That now rides with the lines
rather than living in a terms page nobody opens.

---

## v1.74 — 2026-09-14

**A small house on a big section is not a cheap house**

    "its miles out"

22 Michaels Avenue, Ellerslie. Asking $1,798,000, CV $2,500,000, our valuation
$2.44M, and a sell estimate of **$745,000** printed beside them. Three of our own
numbers on one card and one of them a third of the others.

**Why it fired.** The asking sits $702,000 from the CV, past the 20% band, so
the asking price is distrusted and the listing falls back to sold comparables.
That fallback valued it at floor area times a suburb rate per square metre,
which never looks at the section at all. On a modest dwelling standing on
1,157 m² of Ellerslie that is not a valuation of the property — it is a valuation
of the house with the land thrown in free.

**Like-for-like first.** `matched_sold_price` already matches on beds, baths,
**land** and floor, and the incomplete-CV path directly above it already uses it.
It was simply never called here. Floor rate stays as the last resort, for a
listing with no comparable sales to match against.

**And a floor under the answer, which is the part that generalises.** We arrive
on this path precisely because the asking and the CV disagree, so neither is a
yardstick on its own. But an answer far below **both** is not a third opinion, it
is a method that has failed: whichever of the two is broken, the property is not
worth a fraction of both. Below 60% of the lower of them the estimate is
withdrawn rather than published, and the run log says `below_both_published`
rather than `unresolved`, because "no answer" and "an answer that failed its own
check" send somebody looking in different places.

This path is worst exactly where the product is most used. A big section is what
a developer is looking for, and a big section is what the floor-rate method
ignores.

---

## v1.73 — 2026-09-13

**A researcher now says whether it found anything, and gets one more go**

**The defect in yesterday's coverage heading, found a day later.** Success meant
"wrote some words", so a researcher that searched properly and reported "the
official sources do not publish this" was counted as ANSWERED. That is the
commonest failure of all, and precisely the one the heading was added to
surface — so the count was truthful about crashes and silent about the thing it
existed for.

The researcher declares it now, `FOUND:` or `NOT FOUND:` on the first line, and
the count reads the declaration. Inferring it from the prose is what got it
wrong: "not published anywhere I can see" and "published at 2.5%" are both
words. A reply with no marker is kept as a finding, because discarding a real
one over a missing prefix is the more expensive mistake.

**And one more go, worded differently.** A search that finds nothing has usually
asked in the wrong words rather than asked for something that does not exist —
official sites index their own vocabulary, not the vocabulary of the person
asking. An empty finding is retried once, told to use the terms the source
itself would use and to try a different official source. Exactly once: a
researcher allowed to keep trying will keep trying, and the slowest of the four
already sets the clock for all of them. The cost falls on the questions that
failed, which is where it belongs, and a finding that took two goes says so.

---

## v1.72 — 2026-09-13

**Three findings out of four no longer read like four**

The gap I flagged and then fixed. A reply is written from what is in front of
it, and a question that came back empty simply is not there — so an answer built
on three of four researchers reads as complete, and the person acts on it as
though the fourth had been checked.

The findings are now headed by a count, `3 ANSWERED, 1 NOT`. When anything came
back empty it is listed **above** the findings, where it cannot be skimmed past,
and it is written as an instruction rather than a note: "researcher 3 found
nothing" is a fact a model will happily record and then answer around. The
prompt carries the same rule, so the reply has to say which part could not be
checked.

A clean run says `4 ANSWERED, 0 NOT` and carries no warning about a gap that
does not exist.

---

## v1.71 — 2026-09-13

**The four researchers now have to look up four different things**

    "They should all look up different things to answer the question with the
     most data"

Nothing stopped four near-identical questions being sent. Four researchers then
chase one fact: four calls, one public site hit four times, one answer wearing
four hats — and the three other things worth knowing never looked at. The
breadth the parallelism was added for is exactly what duplicates spend.

**Near-duplicates are detected and run once.** Compared on the subject words two
questions share rather than on their text, because "what is the LVR rule" and
"LVR rules currently" are the same lookup and read nothing alike. The first
version of this compared sorted characters and deduped nothing, which is worse
than no dedupe at all — it reads as a solved problem. A test now fails if the
character comparison would have been enough, so the mechanism cannot quietly
degrade back.

**Each researcher is told what the others are covering** and instructed to stay
in its lane, so even on adjacent topics it does not return the headline fact all
four of them found.

**And the model is told how to split.** Along the question's own seams — what the
rule says, what it costs, how long it takes, what the published numbers show —
not four rewordings of the headline. When a rephrasing is dropped, the reply
names it and says how many researchers are spare, so the answer is to ask
something different rather than the same thing again.

The opposite failure is guarded too, and it is the more expensive one: merging
two genuinely different questions loses a finding and nobody can tell it
happened. "Resource consent fees" and "building consent fees" both get a
researcher.

One known limit, left as it is: hyphenation. "Bright-line" splits into two words
and "brightline" is one, so that pair is not caught. Real, and not worth
contorting the comparison for.

---

## v1.70 — 2026-09-13

**Four researchers at once**

    "we need up to 4 agents looking the more the better"

One agent is right for most of what falls through to the web: a rate, an LVR
rule, what a zone rule says. One fact, one source of record, one search. It is
wrong for a question with several independent parts. "What has changed for
developers in Auckland this year" is really the plan, the consent rules, the
lending rules and the market, and asked one after another the time budget is
gone before the last two. Asked side by side they cost the same wall-clock as
one.

`research_outside` takes up to four narrow questions and runs a researcher on
each, in parallel, then hands the findings back for Ollie to write from.

**A researcher gets the web tool and nothing else.** No database, no
`query_data`, no valuation engine. That is the whole safety argument and it is
tested: a researcher physically cannot reach our data, so it cannot quietly
answer a question about our own market from somebody else's website. Anything
about a property, a suburb we hold sales for, or our market still comes from
Ollie's own tools, and Ollie synthesises. A researcher is a reader, not a second
opinion.

**The key never becomes a tool argument.** The research tool spawns its own model
calls and needs credentials, while `dispatch(name, args)` receives only what the
model filled in. Threading a key through the tool signature would put a secret in
the one place it must never be. It travels in a context variable set at the top
of the run instead, and the tool's schema takes `questions` and nothing else.

**Four, not ten.** Four is the width of a question a person actually asks, and
every extra one is another call against a public site that did not ask to be hit
four times at once. Past four the marginal researcher re-reads what the others
found. The prompt also says when NOT to use it: for a single fact, four
researchers all read the same page and you wait for the slowest.

One researcher failing does not lose the others, and all of them failing is
reported rather than filled in from memory.

**Caught by an existing test.** The new tool was added to the front of the tool
list, which displaced `value_property` from first place — it sits there
deliberately, because given raw SQL and a valuation tool a model will happily
spend six turns writing SQL. Research is the last thing to try, and it is now
last in the list.

---

## v1.69 — 2026-09-13

**Manholes, and a button so the panel can actually be filled**

    "the under ground services have in there but not filling yet"

It was empty because no network is loaded, which the panel says. What it did not
say is how to fix that, and the fix was a shell on the deploy host — the same
friction that made scraping a property page look like the easier option. The
right source is only the easier source once somebody can reach it. There is now
an admin endpoint that takes the service URL, fetches, loads and stamps the
distances, and reports what it found. A partial fetch is refused rather than
loaded: a network missing part of a city computes wrong distances everywhere
with nothing on any screen saying which part.

**A manhole is not a short pipe.**

    "the water, wastewater and stormwater dont go like that tho"

Correct, and the screenshot's legend makes the point: Water, Wastewater,
Wastewater **manhole**. The operator publishes nodes as their own layer and
every plan draws them as an open circle, because a manhole is where a connection
is actually made — the pipe between two of them is just the pipe. A node is
stored here as a run of two identical vertices so one distance routine serves
both, which means it drew as a line of zero length: invisible. Points now come
back flagged and render as circles, and a node carries no bore label, since the
diameter on it describes the pipe it sits on rather than the manhole.

The worked example was redrawn for the same reason. It had three straight mains
crossing the middle of the site, which is not how a network runs: water follows
the carriageway with a lateral into each property, and wastewater runs along the
rear boundaries between properties with a manhole at each junction. The
illustrated distances have been taken off that page — they were placed by hand
and were never measurements.

---

## v1.68 — 2026-09-13

**Underground services on the property map, and one command to load them**

    "thats what we need to do"

Water in blue, wastewater in red, stormwater in green, drawn over the section
and its dimensions the way the operators' own plans draw them — with each run
carrying the operator's own label, `AC50`, `C1100`, material and bore together.
That label is what makes a line a pipe rather than a coloured stripe: a 50 mm
rider and a 1,100 mm trunk look identical otherwise, and only one of them takes
a subdivision.

Off by default and fetched only when switched on. A property page should not
pull a region's pipe network to draw something most people looking at a house do
not need. The mains draw UNDER the boundary, because a pipe that obscures the
thing the panel is about is a worse drawing than no pipe.

The panel says what it is: where the mains run, from the operator's records,
distances to the nearest main rather than a right to connect, **and not a service
locate**. A pipe on a map looks like a located pipe, and the difference is a dug-up
main.

**`scripts/load_geo.py`, because the easy path was not finished.**

    "but its free its just easier to do it that way"

That was a fair point, and it was fair for a reason worth admitting: the fetcher
and the loaders existed and *nothing called them*. So "use the council's service"
meant writing code and scraping meant writing code, and one of those was already
familiar. Now:

    python scripts/load_geo.py pipes stormwater "<the service URL>"
    python scripts/load_geo.py pipes wastewater ./wastewater.geojson
    python scripts/load_geo.py parcels ./auckland-parcels.geojson
    python scripts/load_geo.py zones   ./unitary-plan-zones.geojson
    python scripts/load_geo.py distances

It takes a URL, a GeoJSON file or a spreadsheet, and every command prints what it
found **and what it did not**. A partial fetch is fatal rather than loaded: a
network missing half a city computes wrong distances everywhere with nothing on
any screen to say which half. Pointed at the attribute-table workbook, it names
the problem exactly — 22 rows, no coordinates, an ArcGIS attribute export drops
the shape, re-export with geometry or use the service URL.

---

## v1.67 — 2026-09-13

**Pull the network from the council's own service**

    ".../Stormwater_Connection/FeatureServer/0/query?outFields=*&where=1=1&f=geojson"

Better than a file, because a server that fetches its own data can refresh it,
and because `f=geojson` is specified to come back in WGS84 — so the projection
trap does not arise. The guard stays anyway: "specified to be" and "is" are
different claims, and the cost of being wrong is the whole layer.

**The failure that looks exactly like success.** A FeatureServer will not return
more than its record cap, typically one or two thousand, and it does not fail
when there is more. It answers with a page and a quiet flag. A naive fetch of
Auckland's stormwater comes back with the first couple of thousand pipes looking
like a complete network, and every distance afterwards is measured against a
fraction of the city. The fetcher pages, and pages **in a stable order** — offset
paging over an unordered result is not paging, because the server may return
rows in a different order between requests and the pages then overlap and skip.
It also says whether it finished or stopped at a cap, which are different
answers and only one of them is a network.

**Dropping `f=geojson` is the projection trap through a side door.** Without it
the server answers in Esri's own JSON, in the service's own projection, which for
a New Zealand council is NZTM2000. That is converted here and refused outright if
it is not in degrees, with the fix named.

**A connection layer is points, not pipes**, and a connection point is the better
thing to measure to — it is where you would actually tie in. Point and MultiPoint
geometry now load alongside lines.

**What the sample workbook settled.** 49 columns, every attribute a developer
could want, and no coordinates anywhere — an attribute-table export drops the
shape. So it fixed the NAMES instead, and those are now read rather than guessed:
`Asset Diameter (mm)`, `Pipe Material`, `Asset Owner`, `Asset Status`, and the
pipe depths. Two things it taught that change the answer:

- **Owner decides whether a pipe is a connection at all.** The export carries
  STORMWATER, TRANSPORT and PARKS side by side. Only the first is the public
  network; the others are road and reserve drainage that run past sites they do
  not serve. Counting them would put a main beside almost every section.
- **Decommissioned pipes are still in the file.** Also excluded.

Both are counted and reported rather than dropped quietly: a load saying 22 read
and 3 written is a question, and a load saying 3 is a mystery.

---

## v1.66 — 2026-09-13

**Stormwater, wastewater and water on every section**

    "we just need to add stormwater and water on sections"

The land says what a site could hold. This says whether it can be connected,
which is the question that stops a development after the land has been paid for.
A large, well-zoned, flat, nearly empty parcel with no wastewater within reach is
not a development site. It is a bill.

`service_pipes` holds each network as its published lines. Every parcel is
stamped with the distance to the nearest main of each kind, so the off-market
search returns them and can filter on wastewater.

**A distance, and nothing more.** Whether a connection is permitted turns on the
main's capacity, its depth, the fall available and the operator's approval, none
of which is in any published layer. The fields are named for what they measure,
and a test fails if a `can_connect` or `serviced` field ever appears.

**Null stays null.** Not computed, and nothing within 500 m, are different
answers and a developer walks away from a site on the difference. The wastewater
filter therefore KEEPS the parcels it cannot judge — excluding them would empty
most of the region the first time anybody used it, while looking exactly like
the region having no sites.

**Measured to the pipe, not to its ends.** A main running the length of a street
is nowhere near either of its own ends, and measuring to vertices reports a house
opposite the middle of it as a hundred metres away. Pipes are indexed into 220 m
cells and registered along their length, so a long straight main is found from
the middle of it rather than only near its vertices.

**Two faults found in where the middle of a section is**, both caught by a
servicing test reporting 36.3 m from a pipe the parcel was 40 m from. This point
decides which zone a parcel takes and how far it is from a main.

- A closed ring repeats its first point, so averaging all of them counted that
  corner twice and dragged the centre 3.7 m toward it.
- The average corner is not the centre anyway, and computing the proper centroid
  on raw degrees is mostly floating-point noise — the terms are tiny differences
  of products of coordinates near (-36.9, 174.7) while a section spans 0.0003 of
  a degree. It put the centre 13 m OUTSIDE a 1,400 m² parcel, which on a corner
  site is a whole zone away. Now computed about a local origin.

**And an argument that is not None.** Called directly rather than through HTTP,
an omitted query parameter is FastAPI's `Query` object, which is not None, so it
passed the obvious check and reached the driver as a bind parameter that failed
naming neither the filter nor the endpoint.

---

## v1.65 — 2026-09-13

**The web fallback, corrected: it was too strict in the case that mattered**

    "yes but it it cant find the answer in the data it look on the internet"

v1.64 forbade a web search after any "CANNOT ANSWER YET" block, on the grounds
that such a block means we hold the data and the question missed it. That is
true of most of them and not of all, and the exception is the one that started
this: **days to sell**. The sold file carries no listing date for most of its
rows, so no rephrasing produces an answer, and the person asking was left with
nothing. Under 1.64 Ollie would have asked a question it already knew had no
useful reply.

**The block now says which kind it is, and the model obeys rather than judges.**

- `Looking it up: NO` — a spelling, a filter, a detail nobody gave. The user
  closes it. No search fixes a spelling; it answers a different question
  confidently, about a real place that is not the place they meant.
- `Looking it up: ALLOWED` — our data cannot produce this for anyone, however
  it is phrased. Lead with what we do hold, then look it up, then say whose
  number it is.

The line sits **above** the Ask, not after it, because a gap block has always
ended on the question to put back. That is a tested contract and the right
shape: the last line is the one the model acts on.

**Two sources added, because safe and useless is not the goal.** REINZ, the
industry's own statistics body, whose house price index carries the national
days-to-sell figure our sold file cannot produce; and interest.co.nz, the
published mortgage rate table. Both are sources of record rather than portals
with listings to sell, which is the line that stops their numbers being read as
ours.

**What the web still may never supply**, even from a good source: a figure about
a specific property or listing, a valuation, a median or sale price for a suburb
we hold sales for, a yield, a margin, a comparable, or any count of what is on
the market. An outside statistic may sit alongside ours, attributed. It may
never stand in for one.

---

## v1.64 — 2026-09-12

**Ollie can look something up, and mostly must not**

    "if ollie cant answer a question via our data it then needs to look on the
     internet for the relvent data to answer the question but only if it can
     answer the question from our data"

There is a real gap. Mortgage rates, LVR rules, a plan change, a consent fee,
what a zone rule actually says — all of it bears on a property decision, none of
it is in the database, and the answer today is "I can't help with that", which
is true and useless.

**The hard part is knowing when not to.** "Cannot answer" is two situations
wearing the same words:

- **We hold nothing of that kind.** Nobody has loaded mortgage rates and nobody
  will. Looking it up is right.
- **We hold exactly that and the question missed it.** A suburb spelled
  differently, filters too tight, sold rows with no listing date. Ollie already
  handles this: the tool returns a CANNOT ANSWER YET block naming the one
  missing thing, and the answer is to ask for it.

Searching the web in the second case is worse than staying silent. It would
answer a question about **our** market off somebody else's website — a Grey Lynn
median from a portal rather than out of the sold file every number here is
measured against — and it would read like an answer. So the rule is absolute and
tested: a CANNOT ANSWER YET block never goes to the web.

**Three more guards, all tested.**

- **Official sources only.** The Reserve Bank, Stats NZ, IRD, the legislation,
  the councils, LINZ, Watercare, MBIE, Tenancy Services. No property portal is
  on the list, because their numbers would be quoted interchangeably with ours.
- **Never a figure about our own market.** Valuations, medians, sale prices,
  yields and listing counts come from tools or they do not get said. The web
  tells you what the rules are, never what a property is worth.
- **Never a customer's name, email or saved search** in a query, and any figure
  from outside is named as such in the answer.

**It degrades instead of failing.** The search runs on the provider's side as a
tool block, and this machine cannot reach the API to confirm that block's exact
shape. A rejected tool would have taken down every question rather than one
feature. A refusal that names the tool now drops it and retries once; anything
else — a bad key, a missing model, a rate limit — still surfaces as itself.

---

## v1.63 — 2026-09-12

**Find the sections nobody is selling**

    "if you're a Developer and you're in Grey Lynn and you miss out on a
     property you're looking for a over 1000 m2 zoned Terrace houses can we
     find them?"

That is two stored attributes and a join, and it was unanswerable only because
the rows did not exist. Boundaries were fetched one pin at a time, and a cache
keyed on the pins we happened to look at only ever holds the houses that **were**
for sale, which is the opposite of the question.

`land_parcels` is the other shape of the same data: one row per section in a
region, loaded in bulk, searchable by area and zone. The subdivision engine
never asked whether a house was for sale — it takes a zone, a land area, a title
type and a section rate — so every section now runs on exactly the same rules as
a listing, and the terrace packer from 1.62 tests each one's shape.

`GET /api/offmarket/search` answers the question as asked: suburb, minimum area,
zone. Each result carries the strategy, the yield, and how many actually fit.

**Loaded from a file, not fetched.** The parcel layer is a regional extract and
the zoning comes from a council portal. Both are large, both change rarely, and
neither is reachable from the machine this runs on. Loading from an export also
means the exact bytes behind a result can be kept and re-run.

**Three things it refuses to guess.**

- A parcel with **no zone is not assessed**. The zone is the engine's first gate,
  and a Single House section is never subdividable whatever its size, so filling
  a blank turns a house into a development site on no evidence. They are counted
  and reported: "400 of the 900 sections you searched have no zoning loaded" is
  a fact the searcher needs, and an empty screen is not.
- A parcel under a **special-character overlay** is left out by default and
  counted. In the inner-west this decides most of the answer: a site can be
  zoned for terraces and carry a villa that cannot legally come down.
- **No profit is quoted.** There is no purchase price for a house nobody has
  listed. "This section takes five terraces" is a fact about the land; "this
  section makes $900,000" would need a price nobody has been quoted.

**The projection trap, refused rather than survived.** New Zealand exports are
routinely NZTM2000, and a projected coordinate is only a bigger number — nothing
raises, nothing warns, and the whole suburb lands in the Tasman Sea looking like
it loaded fine. A ring outside the degree range is now rejected with a message
naming the projection and the fix.

**And the loader reports what the file did not carry.** Field names are looked
up through a list of candidate spellings, and a name that matched nothing is
printed as NOTHING FOUND. A load that silently filled nothing looks exactly like
a load that worked, until somebody searches days later.

---

## v1.62 — 2026-09-12

**Lay the terraces out on the actual section, then move them**

    "it woul be cool to try do a muck up on each site"
    "or have it so a developer can edite it move the houses around and diveways"

The terrace yield is land area, less a share for shared access, divided by the
land one terrace needs. It ranks a thousand sites well and it cannot see shape.
Four sections of 980 m² — a wedge, a rectangle, a strip and a square — all
return six, and they hold three, five, six and four.

**Lay out terraces** on the sun panel drops the yield's terraces onto the
surveyed boundary as ordinary buildings. They drag, rotate, resize and save
exactly like the neighbours placed by hand, and the shadows recompute as they
move — no new editor, the one that was already there. Each is 6 × 20 m, the same
120 m² the yield costs a terrace at, so the drawing and the pro-forma cannot
describe different houses. Every one is placed at a real position and then
tested for fitting wholly inside the parcel, rather than counted from a formula.

**It is useful in one direction, and the panel says which.** Fewer than the
arithmetic claimed is a finding: the rectangles genuinely do not fit. As many
only rules out the shape. It does not know which side is the road, so it tries
every boundary as the street and keeps the best. It does not know the zone's
coverage, setbacks, outdoor living space or height to boundary. It does not know
the ground. No surveyed boundary means no layout at all, rather than terraces
drawn on the estimated square.

**Two bugs found while writing it, pulling opposite ways.** Starting the walk on
the boundary put the first test rectangle a metre outside the parcel, so a plain
35 × 28 m section came back as zero lots — which reads as "cannot be developed".
Nudging the start inwards instead spent the nudge out of the width budget, so a
14 m frontage, exactly two 6 m lots and two 1 m margins, came back as one. The
slack now comes off the test rather than the position.

And a third that never shipped: the layout is measured from the listing's pin,
because that is what the building editor measures from. Measured from the
parcel's own centroid it would have arrived correct in shape and wrong in
position, every house shifted by the gap between the pin and the middle of the
section, with nothing on screen to say so.

---

## v1.61 — 2026-09-12

**Section dimensions, as numbers rather than as pixels**

The map has drawn a length against each boundary for a while, on the surveyed
LINZ parcel, in orange. That is where the numbers stopped: on a canvas, in one
panel. They were not in the API response, not in an export, not filterable, and
not available for a site nobody has listed.

    "a developer can look for houses that are zoned right and the dimensions"

That is a filter, and a filter needs numbers as numbers. `/api/geo/parcel` now
returns every side of the section with its length and its bearing, plus the
perimeter and the longest side. Derived from the boundary rather than stored
next to it, so a dimension cannot disagree with the shape it was measured off,
and so the parcels already cached gain their measurements without being
re-fetched.

**The longest side is not called a frontage.** Which side faces the road is a
question about roads, and there is no road data here to answer it with. A field
named `frontage_m` would be a guess wearing a measurement's clothes, and
frontage is precisely the number somebody would act on. A test fails if that
name ever appears.

**A fabricated measurement removed.** Where LINZ has no parcel, the panel draws
a SQUARE of √(land area) so the shade has ground to fall on — and it was
labelling that square's sides exactly like a surveyed boundary. Four confident
figures to one decimal place, every one of them a consequence of assuming the
section is square. The note underneath did say "approximated"; nobody reads a
caption to decide whether the number an arrow points at is real. The estimated
shape now carries no lengths at all, and says what it is.

---

## v1.60 — 2026-09-12

**Days to sell, measured instead of read off a file**

    "we should have how many days to sell"
    "183 sold and you cant asnwer days too sell"

The suburb screen showed 183 sales and a dash where the days figure goes. The
dash was honest. The sold file's days-on-market column is empty for almost all
of it, because 89,262 of its 116,959 rows are rebuilt out of sale-history JSON
and a history entry is a price and a date — there was never a campaign behind it
to measure. No amount of care with that column produces a number that is not in
the file.

**So it is measured from what we watch.** Every listing now carries the day it
first appeared in our book. Paired with the day its advertisement stopped
answering, the gap between them is a days-to-sell we observed first-hand.

- The suburb tile fills in from it when the file has nothing to say. Where the
  file **does** carry a measured campaign, the file still wins — the watched
  figure is a fallback, not a replacement.
- Ollie answers the same question the same way, and when neither source can
  answer yet it says so instead of stopping at "no listing date".
- The workbook and the CSV gained two columns: **First seen by us** and **Days
  advertised**, next to the file's own figure so the two audit each other.

**A wrong number removed.** Rebuilding a property's sale history used to copy
the *current* campaign's days-on-market, listing date and sale method onto every
past sale — so a sale from 1998 carried the 2026 campaign's marketing. That is
not a gap, it is a fabrication, and it is what "how long do houses take to sell"
would have averaged. Those fields are now blanked on rebuilt sales and kept only
on the one entry that is the current sale.

**The fault this could have shipped with.** A house still advertised arrives in
every weekly file, and is written as a new row each time. Left alone, its
first-seen date resets to today on every load, every listing reads as one week
old, and the answer to "how long do houses take to sell" is seven days for
everyone, forever. It would not have looked like a bug. It would have looked
like the fastest market in the country. The date is now carried onto houses we
have seen before, matched on the portal's own identifier **or** the address, so
a listing re-posted under a new campaign is still the house we have been
watching since March.

**The same fault, in eight more places.** `sold_batch_ids()` exists so there is
one answer to "which sold rows count". A dozen places went on working it out
themselves as "the batch with is_active set", which was correct while each
upload replaced the last. Once history began accumulating, each of them quietly
narrowed to whichever delivery arrived most recently — a few hundred sales out
of a hundred thousand. Nothing errored. It surfaced as four unrelated-looking
symptoms:

- Ollie's renovation tool came back with no districts
- Ollie's days-to-sell tool answered out of one file
- the days-to-sell chart drew a short line
- the comparable sales on a property page read like a thin suburb

All now go through the shared helper, along with the sold list, the conversion
dashboard and the address lookup. A test walks the source and fails if any
module reads the sold data as a single batch again.

---

## v1.59 — 2026-09-10

**The dashboard says what is actually on the site**

    "in the dashbaord i need the numbers of what is live and what has been
     deleted via our sold check"
    "and to be able to download all live data in csv"
    "and we want to show what is deleted weekly"

Everything the review screen showed described the batch being **reviewed**. None
of it answered the question an operator opens that page to ask. The week the
book fell from 9,281 listings to 657, the figure was in the database the whole
time and on no screen at all — it was found by a person scrolling a property
list and counting, days later.

**On the site now** — four counts, shown whether or not an upload is in
progress, because most of the time nothing is staged and that is exactly when
the number matters:

- **Live listings** — what a customer can see
- **Off, advertisement gone** — the sold check
- **Held at review**
- **Hidden, other** — no floor area, or a price the scraper invented

They reconcile to the total, and there is a test that fails if they stop doing
so. The live figure is not a restatement of the visibility rule — it calls the
site's own filter, and a test asks the dashboard and the site separately and
fails if the two ever disagree. A dashboard that contradicts the page it
describes is worse than none, because it is believed.

**Taken off, week by week.** One number says the site is smaller; it does not
say whether that is a normal week's churn or something breaking, and those need
opposite responses. Counted once per house however many loads it appeared in —
a listing carried forward for a month is four rows and one departure. A week
with none is absent rather than a zero, because a fabricated zero cannot be told
from a week the check did not run.

**Download live data (CSV).** Every listing in the live load including the ones
held back or taken off, with a column saying which is which. It now shares its
column list with the Excel workbook — they used to keep separate lists, so two
exports of one batch carried different fields under different headings and
adding a column to one silently left the other behind.

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
