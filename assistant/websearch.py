"""Looking something up, but only once our own data has been ruled out.

    "if ollie cant answer a question via our data it then needs to look on the
     internet for the relvent data to answer the question"

There is a real gap. Rates, LVR rules, a plan change, a council consent fee,
what the Unitary Plan actually says about a zone — all of it bears on a property
decision and none of it is in our database. Today Ollie says it cannot help,
which is true and useless.

THE HARD PART IS NOT THE SEARCH. It is knowing when NOT to. "Cannot answer" is
two completely different situations wearing the same words:

    We hold nothing of that kind. Nobody has loaded mortgage rates and nobody
    will — it is not property data we collect. Looking it up is the right move.

    We hold exactly that, and the question missed it. The suburb was spelled
    differently, the filters were too tight, the sold file has no listing date.
    Ollie already handles this: a tool returns a "CANNOT ANSWER YET" block
    naming the ONE missing thing, and the answer is to ask for it.

Going to the internet in the second case is worse than saying nothing. It would
answer a question about OUR market from somebody else's website — a median for
Grey Lynn off a portal rather than out of the sold file we measure everything
against — and it would look like an answer. The whole product is a promise that
a number came from data we hold. So the rule is absolute and it is tested: a
CANNOT ANSWER YET block never goes to the web.

TWO MORE GUARDS.

Restricted to sources worth quoting. An open web search on a property question
returns portals, blogs and agencies, all of which have numbers and none of which
we can stand behind. The allow-list below is official and statutory — the
Reserve Bank, Stats NZ, the councils, LINZ, the legislation — so what comes back
is a rule or a rate, which is what was missing, rather than somebody else's
estimate of a house.

Never a figure about our own market. Even from a good source: listing counts,
valuations, medians and comps come from tools or they do not get said. The web
answers what the rules ARE, never what a property IS worth.

WHY THIS DEGRADES INSTEAD OF FAILING. The search runs on the provider's side,
declared as a tool block. The machine this was written on cannot reach the API
to confirm the block's exact shape, and a rejected tool would take down every
question rather than just this feature. So a refusal that names the tool drops
it and retries once without it — Ollie loses the lookup and keeps working.
"""
from __future__ import annotations

# The server-side search tool. Available on Opus 4.6 and newer, which covers the
# model this runs on; older models take the earlier `web_search_20250305` and
# would need this constant changed rather than the logic.
TOOL_TYPE = "web_search_20260209"
TOOL_NAME = "web_search"

# Three lookups is a fact-check, not a research session. The question has
# already failed against our data by this point, and a model given ten searches
# will use ten.
MAX_USES = 3

# Sources whose answer is the answer, rather than sources that have an opinion
# about it. Official, statutory, or the body that sets the thing being asked
# about. A property portal is deliberately absent: its numbers compete with
# ours, and "competing" is the polite word for "would be quoted interchangeably".
ALLOWED_DOMAINS = [
    "rbnz.govt.nz",             # official cash rate, LVR and DTI rules
    "stats.govt.nz",            # official statistics
    "ird.govt.nz",              # bright-line, tax treatment
    "legislation.govt.nz",      # the acts themselves
    "linz.govt.nz",             # titles, survey, the cadastre
    "aucklandcouncil.govt.nz",  # Unitary Plan, consents, rates, fees
    "watercare.co.nz",          # water and wastewater connection rules
    "building.govt.nz",         # MBIE building code and consent guidance
    "tenancy.govt.nz",          # tenancy rules behind a yield question
    "environmentguide.org.nz",  # RMA process, plainly explained
    # The two that make the fallback actually useful rather than merely safe.
    # REINZ is the industry's own statistics body and its house price index is
    # the standard national measure, including the days-to-sell figure our sold
    # file cannot produce. interest.co.nz is the standard published mortgage
    # rate table. Both are sources of record, not portals with listings to sell,
    # which is the line that keeps their numbers from being read as ours.
    "reinz.co.nz",
    "interest.co.nz",
]


def tool_block(max_uses: int = MAX_USES) -> dict:
    """The tool declaration handed to the provider."""
    return {
        "type": TOOL_TYPE,
        "name": TOOL_NAME,
        "max_uses": max_uses,
        "allowed_domains": list(ALLOWED_DOMAINS),
    }


def is_tool_rejection(exc: Exception) -> bool:
    """Did the provider refuse the request BECAUSE of the search tool?

    Told apart from every other 400 deliberately. A key problem, a bad model
    name or an oversized request must still surface as itself — retrying those
    without the tool would hide the real fault behind a feature nobody asked
    about.
    """
    text = f"{exc}".lower()
    if "400" not in text and "invalid_request" not in text and \
            "bad request" not in text:
        return False
    return (TOOL_TYPE in text or TOOL_NAME in text
            or "web_search" in text or "allowed_domains" in text)


# The rules the model is held to. Kept here beside the tool rather than buried
# in the system prompt, so the guard and the thing it guards cannot drift apart
# — and so a test can assert the prompt still carries them.
SYSTEM_RULES = """
LOOKING SOMETHING UP OUTSIDE OUR DATA
- You have a web_search tool, restricted to official and statutory sources: the
  Reserve Bank, Stats NZ, IRD, the councils, LINZ, the legislation, Watercare,
  MBIE, Tenancy Services, REINZ, interest.co.nz.
- Try our own tools FIRST, every time. The web is what you reach for once our
  data has genuinely been ruled out, never instead of asking it.
- Then use it for anything our data does not hold: interest and mortgage rates,
  LVR or DTI rules, bright-line and tax treatment, what a plan or zone rule
  actually says, consent processes and fees, tenancy law, and published
  market-wide statistics we do not compute ourselves.

- A "CANNOT ANSWER YET" block ENDS WITH A LINE THAT TELLS YOU WHICH IT IS.
  Read it and obey it; do not decide for yourself.
    "Looking it up: NO" — we hold this and the question missed it. A spelling, a
    filter, a detail nobody gave. Searching would answer a DIFFERENT question
    confidently. Put its Ask to the user and stop.
    "Looking it up: ALLOWED" — our data cannot produce this for anyone, however
    it is phrased. Lead with the "Already known" figure, then search, then say
    whose number you are quoting.

- WHAT THE WEB MAY NEVER SUPPLY, even from a good source: a figure about a
  specific property or listing; a valuation; a median or a sale price for a
  suburb we hold sales for; a yield; a margin; a comparable; or any count of
  what is on the market. Those come from tools or they do not get said, and
  this is the rule the whole product rests on — the web tells you what the
  RULES are, never what a property IS worth. An outside market statistic may
  sit ALONGSIDE ours, clearly attributed; it may never stand in for one.
- FOUR AT ONCE, when the question has four parts. A question like "what has
  changed for developers in Auckland this year" is really the plan, the consent
  rules, the lending rules and the market — four independent lookups. Asked one
  after another they spend the whole budget on the first two; asked side by side
  with research_outside they cost the same wall-clock as one. Split it into up
  to four NARROW questions, one per researcher.
  Use research_outside only when the parts are genuinely independent. For a
  single fact, search directly — four researchers on one fact all read the same
  page and you wait for the slowest.
  THE FOUR MUST LOOK UP DIFFERENT THINGS. Split along the question's own seams —
  what the rule says, what it costs, how long it takes, what the published
  numbers show — never four rewordings of the headline. Near-duplicates are
  detected and run once, and the reply tells you how many researchers you wasted
  and which ones; if it does, spend them on ground nobody covered.
  WHAT CAME BACK EMPTY IS PART OF YOUR ANSWER. The findings are headed by a
  count and, when anything failed, a "NOT ESTABLISHED" list. Say which part you
  could not check, in the reply, in a sentence. An answer that quietly covers
  three of four questions reads as complete and is not — and the person acts on
  it as though the fourth had been checked.
  The researchers cannot see our database, by design. Anything about a property,
  a suburb we hold sales for, or our own market still comes from the tools above,
  and you synthesise: they hand you findings, you write the answer.
- NEVER put a customer's name, email, or saved search into a search.
- Whenever you use it, say in the answer that the figure is from outside our
  data and name the source, so nobody mistakes it for one of ours.
- If it finds nothing useful, say so and stop. Do not fill the gap from memory.
"""
