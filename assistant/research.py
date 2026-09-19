"""Four researchers at once, for the questions one search cannot answer.

    "we need up to 4 agents looking the more the better"

Ollie is one agent working one question, and for most of what falls through to
the web that is right: a rate, an LVR rule, what a zone rule says. One fact,
one source of record, one search. A second agent would read the same page.

It is wrong for a question with several independent parts. "What has changed
for developers in Auckland this year" is really four questions — the plan, the
consent rules, the lending rules, the market — and asking them one after another
inside a single conversation spends the whole time budget on the first two and
answers the rest badly or not at all. Asked side by side they cost the same
wall-clock as one.

WHAT MAKES THIS SAFE TO ADD. Each researcher is the same model under the SAME
rules Ollie is: official sources only, never a figure about a property or a
market we hold data for, never a customer's details. They are given the web
tool and NOTHING ELSE — no database, no query_data, no valuation engine. That is
deliberate and it is the whole safety argument: a researcher physically cannot
reach our data, so it cannot quietly answer a question about our market from
somebody else's website. Everything about us still comes from Ollie's own tools.

They also do not talk to each other and they do not decide anything. Each
returns a short written answer with its sources, and Ollie writes the reply. A
researcher is a reader, not a second opinion.

WHY FOUR AND NOT TEN. Four is the width of a question a person actually asks,
and every extra one is another model call against a public site that did not ask
to be hit four times a second. Past four the marginal agent re-reads what the
others found.
"""
from __future__ import annotations

import concurrent.futures as _futures
import json
from dataclasses import dataclass

from . import websearch

MAX_AGENTS = 4
# One researcher's ceiling. Generous, because they run side by side and the
# whole point is that the slowest one sets the clock rather than the sum.
PER_AGENT_SECONDS = 90.0
# Short on purpose. A researcher hands Ollie findings to write from, not a
# finished answer — four essays would blow the context the reply is written in,
# and Ollie would quote one of them instead of writing.
MAX_TOKENS = 900

# Two questions sharing this much of their subject are one question. Four
# researchers chasing the same fact is the failure this whole thing exists to
# avoid: four calls, one public site hit four times, one answer wearing four
# hats, and the three other things worth knowing never looked at.
#
# Compared on the SUBJECT WORDS they share, not on their text. "What is the LVR
# rule" and "LVR rules currently" are the same lookup and read nothing alike —
# matching their characters, in any order, does not catch them, which is how the
# first version of this shipped a dedupe that deduped nothing.
SAME_QUESTION = 0.60

# Words that say nothing about WHICH lookup this is. Left in, every question
# looks like every other one, because they all ask what and how.
_NOISE = frozenset((
    "a an the and or of for in on at to is are was were be been do does did how"
    " what when where which who why not no any some this that these those with"
    " from by as it its currently current now new nz zealand auckland i we you"
    " please can could would should about into over under more most much many"
).split())

# Angles worth splitting a question along, offered to the model as a prompt
# rather than imposed. A broad question almost always decomposes this way, and
# naming the axes is what stops four researchers being handed four rephrasings
# of the headline.
ANGLES = ("what the rule actually says",
          "what it costs and who charges it",
          "how long it takes and what the process is",
          "what the published numbers show")

SYSTEM = """You are a researcher for a New Zealand property analyst. You are \
given ONE narrow question and a web search tool, and you answer only that.

- Search only the official sources you are restricted to. If they do not answer
  it, say so plainly. Do not fill a gap from memory.
- Quote the figure, the rule or the date, and NAME the source and its date. A
  finding nobody can check is not a finding.
- You do NOT have access to the analyst's property database and must not
  pretend to. Never state a valuation, a median, a sale price, a yield or a
  count of listings for any suburb: those come from their own data, not from
  you. If the question asks for one, say it is not yours to answer.
- BEGIN YOUR ANSWER WITH "FOUND:" OR "NOT FOUND:", on the first line, before
  anything else. "NOT FOUND:" when the official sources do not carry it, when
  what you found is too old to rely on, or when the question is not yours to
  answer. This is not politeness — the person writing the reply counts these,
  and has to tell the reader which part could not be checked. Saying you could
  not find something in prose, without the marker, is counted as having found
  it, and the gap disappears.
- Six sentences at most. You are handing over findings for somebody else to
  write from, not writing the reply.
- You are one of several researchers working at the same time, and you will be
  told what the others are covering. STAY IN YOUR LANE: do not answer their
  questions, even if your search turns up the answer. Four researchers returning
  the same fact is four calls for one finding, and the person writing the reply
  gets breadth from you covering ground nobody else is."""


@dataclass
class Finding:
    question: str
    answer: str
    ok: bool = True
    tried_twice: bool = False


def distinct(questions: list[str]) -> tuple[list[str], list[str]]:
    """Keep the questions that cover different ground; report what was dropped.

        "They should all look up different things"

    Nothing stopped four near-identical questions being sent. Four researchers
    then chase one fact: four model calls, one public site hit four times, and
    one answer coming back wearing four hats — while the three other things
    worth knowing go unlooked-at. The breadth the parallelism was added for is
    exactly what duplicates spend.

    Compared on normalised text rather than exact match, because "what is the
    LVR rule" and "LVR rules currently" are the same lookup and no amount of
    exact matching would ever catch them.
    """
    import re

    def subject(q: str) -> set[str]:
        """The words that say WHICH lookup this is."""
        words = re.findall(r"[a-z0-9]+", q.lower())
        out = set()
        for w in words:
            if w in _NOISE or len(w) < 2:
                continue
            # Crudest possible stemming, and enough: rule/rules, fee/fees,
            # consent/consents. Anything cleverer needs a dependency for a
            # comparison of two short sentences.
            out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
        return out

    def same(a: set[str], b: set[str]) -> float:
        """Share of the subject the two have in common."""
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    kept: list[str] = []
    kept_subjects: list[set[str]] = []
    dropped: list[str] = []
    for q in questions:
        subj = subject(q)
        if not subj:
            continue
        if any(same(subj, s0) >= SAME_QUESTION for s0 in kept_subjects):
            dropped.append(q)
        else:
            kept.append(q)
            kept_subjects.append(subj)
    return kept, dropped


def _read(question: str, text: str) -> Finding:
    """A researcher's reply, and whether it actually found anything.

    THE DEFECT THIS EXISTS TO FIX. Success used to mean "wrote some words", so
    a researcher that searched properly and reported "the official sources do
    not publish this" counted as ANSWERED — which is the commonest failure of
    all, and the one the coverage heading was added to surface. The heading was
    therefore truthful about crashes and silent about the thing it was for.

    So the researcher DECLARES it, on the first line, and this reads the
    declaration. Inferring it from the prose is what got it wrong: "not
    published anywhere I can see" and "published at 2.5%" are both words.

    A reply with no marker at all is treated as found, because the alternative —
    discarding a real finding over a missing prefix — is the more expensive
    mistake, and the prompt asks for it plainly enough that it is rare.
    """
    body = text.strip()
    if not body:
        return Finding(question=question, answer="Nothing came back.", ok=False)
    head = body.split("\n", 1)[0].strip().upper()
    if head.startswith("NOT FOUND"):
        rest = body.split(":", 1)[1].strip() if ":" in body.split("\n", 1)[0] else ""
        return Finding(question=question, ok=False,
                       answer=rest or "The official sources do not carry this.")
    if head.startswith("FOUND"):
        rest = body.split(":", 1)[1].strip() if ":" in body.split("\n", 1)[0] else body
        return Finding(question=question, answer=rest or body, ok=True)
    return Finding(question=question, answer=body, ok=True)


def _one(question: str, api_key: str, model: str,
         workspace_id: str | None, others: list[str] | None = None) -> Finding:
    """One researcher, one question, one shot.

    `others` is what the rest are covering. Told rather than inferred: a
    researcher that knows the neighbouring questions stops answering them, and
    a researcher that does not will happily return the headline fact all four
    of them found.
    """
    import anthropic

    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
    client = anthropic.Anthropic(api_key=api_key, timeout=PER_AGENT_SECONDS,
                                 max_retries=1, default_headers=headers)
    ask = question
    if others:
        ask = (f"{question}\n\nOther researchers are covering these, so leave "
               f"them alone and answer only the question above:\n"
               + "\n".join(f"- {o}" for o in others))

    def attempt(prompt: str) -> Finding:
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM,
            tools=[websearch.tool_block()],
            messages=[{"role": "user", "content": prompt}],
        )
        return _read(question, "\n".join(
            b.text for b in resp.content
            if getattr(b, "type", "") == "text").strip())

    try:
        got = attempt(ask)
        if got.ok:
            return got
        # ONE MORE GO, WORDED DIFFERENTLY.
        #
        # A search that finds nothing has usually asked in the wrong words, not
        # asked for something that does not exist — official sites index their
        # own vocabulary, not the vocabulary of the person asking. One retry
        # roughly halves the empty findings at the cost of a second call on the
        # ones that failed, which is where the cost belongs.
        #
        # Exactly one. A researcher allowed to keep trying will keep trying, and
        # the slowest one already sets the clock for all four.
        second = attempt(
            f"{ask}\n\nYour first search found nothing usable. Try again with "
            f"DIFFERENT wording — the terms the source itself would use rather "
            f"than the terms of the question — and a different official source "
            f"if one could carry it. If it genuinely is not published, say NOT "
            f"FOUND and stop.")
        second.tried_twice = True
        return second
    except Exception as exc:                                  # noqa: BLE001
        if websearch.is_tool_rejection(exc):
            # Same rule as the main loop: losing the search tool costs the
            # lookup, never the question.
            return Finding(question=question, ok=False,
                           answer="The search tool was refused for this one.")
        return Finding(question=question, ok=False,
                       answer=f"Could not research this: {type(exc).__name__}")


def research(questions: list[str], *, api_key: str, model: str,
             workspace_id: str | None = None) -> str:
    """Run up to four researchers side by side and return what they found.

    Returns text for the model to write from, not JSON to be echoed: the reply
    is Ollie's to write, and handing back a structured object invites it being
    printed at the customer instead.
    """
    asked = [q.strip() for q in (questions or []) if str(q).strip()]
    qs, same = distinct(asked)
    qs = qs[:MAX_AGENTS]
    if not qs:
        return ("No questions were given to research. Say what you need looked "
                "up, one line each — and make them four DIFFERENT things: "
                + "; ".join(ANGLES) + ".")
    if not api_key:
        return "No key is configured, so nothing could be looked up."

    out: list[Finding] = []
    # Threads rather than async: the SDK call is blocking, the whole job is
    # waiting on somebody else's server, and four threads that spend their lives
    # in a socket read cost nothing worth structuring the loop around.
    with _futures.ThreadPoolExecutor(max_workers=len(qs)) as pool:
        futures = [pool.submit(_one, q, api_key, model, workspace_id,
                               [o for o in qs if o != q]) for q in qs]
        for f in futures:
            try:
                out.append(f.result(timeout=PER_AGENT_SECONDS + 15))
            except Exception:                                  # noqa: BLE001
                out.append(Finding(question="(one researcher)", ok=False,
                                   answer="Timed out."))

    # WHAT CAME BACK EMPTY IS PART OF THE ANSWER.
    #
    # Three findings out of four read exactly like four: the reply is written
    # from what is in front of it, and a question that returned nothing simply
    # is not. So the gap is stated at the TOP, before the findings, where it
    # cannot be skimmed past — and it is stated as a instruction, because
    # "researcher 3 found nothing" is a fact the model will happily note and
    # then answer around.
    answered = [f for f in out if f.ok]
    empty = [f for f in out if not f.ok]

    lines = [f"RESEARCHED {len(out)} QUESTION(S) OUTSIDE OUR DATA — "
             f"{len(answered)} ANSWERED, {len(empty)} NOT.",
             "Each answer is from official sources only and none of them can "
             "see our database. Name the source when you use one, and take "
             "anything about our own market from our own tools."]
    if empty:
        lines.append("")
        lines.append("NOT ESTABLISHED, and your reply must SAY SO rather than "
                     "answer around it:")
        for f in empty:
            twice = " (searched twice)" if f.tried_twice else ""
            lines.append(f"  · {f.question}{twice} — {f.answer}")
        lines.append("Tell the person which part you could not check. An answer "
                     "that quietly covers three of four questions reads as "
                     "complete and is not.")
    lines.append("")
    for f in answered:
        lines.append(f"— {f.question}")
        lines.append(f"  {f.answer}")
    if same:
        # Named, not silently dropped. A researcher that did not run is a thing
        # the person asking might want run properly, and the fix is to ask a
        # different question rather than the same one again.
        lines.append("")
        lines.append(
            f"{len(same)} more asked the same thing as one above and were not "
            f"run twice: " + "; ".join(f"{q!r}" for q in same) + ". "
            f"You have {MAX_AGENTS - len(out)} researcher(s) spare — spend them "
            f"on different ground ({'; '.join(ANGLES)}) if it would help.")
    if not answered:
        lines.append("")
        lines.append("Nothing usable came back. Say so rather than filling it in.")
    return "\n".join(lines)
