"""Four researchers at once, and the four ways that could go wrong.

    "we need up to 4 agents looking the more the better"

One agent is right for most of what falls through to the web: a rate, an LVR
rule, what a zone rule says. One fact, one source of record. It is wrong for a
question with several independent parts — "what has changed for developers in
Auckland this year" is the plan, the consent rules, the lending rules and the
market, and asked one after another the budget is gone before the last two.

Adding agents adds four risks, and the tests below are mostly those:

    A researcher answering about OUR market. This is the one that would undo
    the product. They are given the web tool and NOTHING ELSE — no database, no
    query_data, no valuation engine — so a researcher physically cannot reach
    our data and cannot quietly answer a Grey Lynn median off somebody's
    website.

    The key reaching the model. The research tool spawns its own calls and
    needs credentials, and `dispatch(name, args)` takes only what the model
    filled in. A key threaded through the tool signature would be a secret in
    an argument a language model writes.

    More agents than asked for, on a public site that did not ask to be hit
    four times at once.

    One slow researcher holding the whole answer.
"""
from __future__ import annotations

import pytest

from app.assistant import research, websearch


# ---- the shape -----------------------------------------------------------
def test_four_is_the_ceiling():
    assert research.MAX_AGENTS == 4


def test_more_than_four_questions_are_cut_to_four(monkeypatch):
    seen: list[str] = []

    def fake(q, key, model, ws, others=None):
        seen.append(q)
        return research.Finding(question=q, answer="ok")

    monkeypatch.setattr(research, "_one", fake)
    research.research([f"q{i}" for i in range(9)], api_key="k", model="m")
    assert len(seen) == 4, f"{len(seen)} researchers were started"


def test_no_questions_is_said_rather_than_four_empty_agents(monkeypatch):
    monkeypatch.setattr(research, "_one",
                        lambda *a, **k: pytest.fail("a researcher was started"))
    out = research.research([], api_key="k", model="m")
    assert "No questions" in out


def test_a_blank_question_is_not_a_researcher(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(research, "_one", lambda q, *a: (
        seen.append(q) or research.Finding(question=q, answer="ok")))
    research.research(["rates", "   ", "", "consent fees"], api_key="k", model="m")
    assert seen == ["rates", "consent fees"]


def test_no_key_is_said_rather_than_four_failures(monkeypatch):
    monkeypatch.setattr(research, "_one",
                        lambda *a, **k: pytest.fail("a researcher was started"))
    assert "No key" in research.research(["rates"], api_key="", model="m")


# ---- a researcher cannot reach our data ----------------------------------
def test_a_researcher_gets_the_web_tool_and_nothing_else():
    """THE ONE THAT MATTERS. A researcher with query_data could answer a
    question about our own market off somebody else's website, which is the
    one thing the whole product promises it will not do. It cannot, because it
    is never handed a tool that reaches the database."""
    import inspect

    src = inspect.getsource(research._one)
    assert "websearch.tool_block()" in src
    for forbidden in ("TOOL_SPECS", "dispatch", "query_data", "SessionLocal"):
        assert forbidden not in src, (
            f"a researcher was given {forbidden} — it can now reach our data")


def test_its_instructions_forbid_quoting_our_own_figures():
    s = " ".join(research.SYSTEM.split())
    assert "do NOT have access to the analyst's property database" in s
    for word in ("valuation", "median", "sale price", "yield"):
        assert word in s, f"a researcher is not told to refuse a {word}"


def test_it_is_told_to_name_the_source():
    assert "NAME the source" in research.SYSTEM


def test_a_researcher_answer_is_short_enough_to_synthesise_from():
    """Four essays would blow the context the reply is written in, and the
    model would quote one of them instead of writing an answer."""
    assert research.MAX_TOKENS <= 1200
    assert "Six sentences at most" in research.SYSTEM


# ---- the key never becomes a tool argument -------------------------------
def test_the_tool_takes_questions_and_nothing_else():
    """A key in a tool's schema is a secret in the one place it must never be:
    an argument a language model fills in."""
    from app.assistant.tools import TOOL_SPECS

    spec = next(t for t in TOOL_SPECS if t["name"] == "research_outside")
    assert set(spec["parameters"]["properties"]) == {"questions"}


def test_the_key_comes_from_the_run_not_from_the_model():
    import inspect

    from app.assistant import tools

    src = inspect.getsource(tools.research_outside)
    assert "CALLER.get()" in src, "the researchers are not using the run's key"


def test_a_provider_without_the_search_tool_says_so(monkeypatch):
    """The researchers use the server-side search tool, which is an Anthropic
    feature. Said plainly rather than failing — the question is still
    answerable, just not this way."""
    from app.assistant import tools
    from app.assistant.providers import CALLER

    CALLER.set({"provider": "openai", "api_key": "k", "model": "gpt-5"})
    out = tools.research_outside(["rates"])
    assert "not available on this provider" in out


# ---- failures do not take the answer down --------------------------------
def test_one_researcher_failing_does_not_lose_the_others(monkeypatch):
    def fake(q, key, model, ws, others=None):
        if q == "bad":
            raise RuntimeError("boom")
        return research.Finding(question=q, answer=f"found {q}")

    monkeypatch.setattr(research, "_one", fake)
    out = research.research(["good", "bad", "also good"], api_key="k", model="m")
    assert "found good" in out and "found also good" in out


def test_all_of_them_failing_is_reported_not_papered_over(monkeypatch):
    monkeypatch.setattr(research, "_one", lambda q, *a: research.Finding(
        question=q, answer="Could not research this.", ok=False))
    out = research.research(["consent fees", "LVR limits"],
                            api_key="k", model="m")
    assert "Nothing usable came back" in out
    assert "rather than filling it in" in out


def test_what_comes_back_is_text_to_write_from_not_json(monkeypatch):
    """Handing back a structured object invites it being printed at the
    customer instead of answered."""
    monkeypatch.setattr(research, "_one", lambda q, *a: research.Finding(
        question=q, answer="The rule is X, per rbnz.govt.nz, March 2026."))
    out = research.research(["what is the LVR rule"], api_key="k", model="m")
    assert not out.strip().startswith("{")
    assert "rbnz.govt.nz" in out


# ---- and the model is told when NOT to use it ----------------------------
def test_the_prompt_warns_against_four_agents_on_one_fact():
    rules = " ".join(websearch.SYSTEM_RULES.split())
    assert "research_outside" in rules
    assert "four researchers on one fact all read the same page" in rules
    assert "genuinely independent" in rules


# ---- four DIFFERENT things ---------------------------------------------------
#
#     "They should all look up different things to answer the question with the
#      most data"
#
# Nothing stopped four near-identical questions being sent. Four researchers
# then chase one fact: four calls, one public site hit four times, one answer
# wearing four hats — and the three other things worth knowing never looked at.
# The breadth the parallelism was added for is exactly what duplicates spend.

def test_a_rephrasing_does_not_get_its_own_researcher():
    """THE ONE THAT MATTERS. These read nothing alike and are one lookup."""
    kept, dropped = research.distinct([
        "what is the LVR rule",
        "LVR rules currently",
        "consent fees in Auckland",
        "how long does a resource consent take",
    ])
    assert len(kept) == 3, kept
    assert dropped == ["LVR rules currently"]


def test_matching_on_text_alone_would_not_have_caught_it():
    """Why this is compared on subject words rather than characters. The first
    version sorted the tokens and compared the strings, which deduped nothing —
    a dedupe that silently dedupes nothing is worse than none, because it reads
    as a solved problem."""
    import difflib

    a, b = "what is the LVR rule", "LVR rules currently"
    text_similarity = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
    assert text_similarity < research.SAME_QUESTION, (
        "the character comparison would have caught it after all, so this test "
        "no longer says anything")
    assert research.distinct([a, b])[1] == [b]


def test_genuinely_different_questions_all_get_a_researcher():
    """The opposite failure, and the more expensive one: merging two real
    questions loses a finding and nobody can tell it happened."""
    kept, dropped = research.distinct([
        "resource consent fees",
        "building consent fees",
        "LVR limits",
        "REINZ house price index",
    ])
    assert len(kept) == 4, f"{dropped} were wrongly treated as duplicates"


def test_a_question_of_nothing_but_filler_is_not_a_researcher():
    kept, _ = research.distinct(["what is it", "how about that", "bright-line test"])
    assert kept == ["bright-line test"]


def test_each_researcher_is_told_what_the_others_are_covering(monkeypatch):
    """Told rather than inferred. A researcher that does not know the
    neighbouring questions will happily return the headline fact all four of
    them found."""
    seen: dict[str, list[str]] = {}

    def fake(q, key, model, ws, others=None):
        seen[q] = list(others or [])
        return research.Finding(question=q, answer="ok")

    monkeypatch.setattr(research, "_one", fake)
    research.research(["consent fees", "LVR limits", "THAB height"],
                      api_key="k", model="m")
    assert seen["consent fees"] == ["LVR limits", "THAB height"]
    assert "consent fees" not in seen["consent fees"]


def test_it_is_told_to_stay_in_its_lane():
    assert "STAY IN YOUR LANE" in research.SYSTEM


def test_wasted_researchers_are_named_not_silently_dropped(monkeypatch):
    """A researcher that did not run is one the person might want run properly,
    and the fix is a different question rather than the same one again."""
    monkeypatch.setattr(research, "_one", lambda q, *a, **k: research.Finding(
        question=q, answer="found it"))
    out = research.research(["the bright-line test",
                             "bright line test rules"],
                            api_key="k", model="m")
    assert "same thing" in out
    assert "researcher(s) spare" in out


def test_the_model_is_told_the_four_must_differ():
    from app.assistant.tools import TOOL_SPECS

    spec = next(t for t in TOOL_SPECS if t["name"] == "research_outside")
    desc = " ".join(spec["parameters"]["properties"]["questions"]["description"].split())
    assert "DIFFERENT THINGS" in desc
    assert "rewordings" in desc

    rules = " ".join(websearch.SYSTEM_RULES.split())
    assert "THE FOUR MUST LOOK UP DIFFERENT THINGS" in rules


# ---- what came back empty is part of the answer ------------------------------
#
# Three findings out of four read exactly like four. The reply is written from
# what is in front of it, and a question that returned nothing simply is not —
# so the answer quietly covers three quarters of the question and the person
# acts on it as though the fourth had been checked.

def test_the_heading_says_how_many_actually_answered(monkeypatch):
    monkeypatch.setattr(research, "_one", lambda q, *a, **k: research.Finding(
        question=q, answer="Nothing found." if "consent" in q else "Found it.",
        ok="consent" not in q))
    out = research.research(["LVR limits", "resource consent fees",
                             "REINZ days to sell"], api_key="k", model="m")
    assert "2 ANSWERED, 1 NOT" in out


def test_the_gap_is_stated_before_the_findings(monkeypatch):
    """At the top, where it cannot be skimmed past. Underneath four findings it
    is a footnote on something that already reads as complete."""
    monkeypatch.setattr(research, "_one", lambda q, *a, **k: research.Finding(
        question=q, answer="Nothing found." if "consent" in q else "Found it.",
        ok="consent" not in q))
    out = research.research(["LVR limits", "resource consent fees"],
                            api_key="k", model="m")
    assert out.index("NOT ESTABLISHED") < out.index("Found it.")


def test_it_is_an_instruction_not_a_note(monkeypatch):
    """"Researcher 3 found nothing" is a fact the model will note and then
    answer around. It has to be told what to do about it."""
    monkeypatch.setattr(research, "_one", lambda q, *a, **k: research.Finding(
        question=q, answer="Nothing found.", ok=False))
    out = research.research(["resource consent fees", "LVR limits"],
                            api_key="k", model="m")
    assert "must SAY SO rather than answer around it" in out


def test_nothing_is_said_when_everything_came_back(monkeypatch):
    """A clean run must not carry a warning about a gap that does not exist."""
    monkeypatch.setattr(research, "_one", lambda q, *a, **k: research.Finding(
        question=q, answer="Found it."))
    out = research.research(["LVR limits", "consent fees"], api_key="k", model="m")
    assert "NOT ESTABLISHED" not in out
    assert "2 ANSWERED, 0 NOT" in out


def test_the_prompt_tells_the_model_to_name_the_gap():
    rules = " ".join(websearch.SYSTEM_RULES.split())
    assert "WHAT CAME BACK EMPTY IS PART OF YOUR ANSWER" in rules
    assert "reads as complete and is not" in rules


# ---- found, or not found, declared rather than guessed -----------------------
#
# The defect in the coverage heading, found the day after shipping it. Success
# meant "wrote some words", so a researcher that searched properly and reported
# "the official sources do not publish this" counted as ANSWERED — which is the
# commonest failure of all, and precisely the one the heading existed to
# surface. It was truthful about crashes and silent about the thing it was for.

def test_a_researcher_declaring_not_found_is_counted_as_not_found():
    got = research._read("q", "NOT FOUND: Auckland Council does not publish it.")
    assert got.ok is False
    assert "does not publish" in got.answer


def test_a_researcher_declaring_found_is_counted_as_found():
    got = research._read("q", "FOUND: LVR is 20%, per rbnz.govt.nz, March 2026.")
    assert got.ok is True
    assert got.answer.startswith("LVR is 20%")


def test_an_empty_reply_is_not_a_finding():
    assert research._read("q", "   ").ok is False


def test_an_unmarked_reply_is_kept_rather_than_discarded():
    """The fallback, and the right way round: discarding a real finding over a
    missing prefix is the more expensive mistake."""
    got = research._read("q", "The rate is 2.5%, per ird.govt.nz.")
    assert got.ok is True


def test_the_researcher_is_told_the_marker_is_counted():
    s = " ".join(research.SYSTEM.split())
    assert 'BEGIN YOUR ANSWER WITH "FOUND:" OR "NOT FOUND:"' in s
    assert "counted as having found it" in s


def test_a_not_found_reply_reaches_the_heading_as_not_answered(monkeypatch):
    """End to end: the declaration has to travel all the way to the count."""
    monkeypatch.setattr(research, "_one", lambda q, *a, **k: research._read(
        q, "NOT FOUND: nothing published." if "consent" in q
        else "FOUND: the rate is 2.5%."))
    out = research.research(["LVR limits", "resource consent fees"],
                            api_key="k", model="m")
    assert "1 ANSWERED, 1 NOT" in out
    assert "NOT ESTABLISHED" in out


# ---- one more go, worded differently ----------------------------------------
def test_an_empty_finding_is_searched_once_more(monkeypatch):
    """A search that finds nothing has usually asked in the wrong words. Official
    sites index their own vocabulary, not the asker's."""
    calls: list[str] = []

    class _Resp:
        def __init__(self, t):
            self.content = [type("B", (), {"type": "text", "text": t})()]

    class _Msgs:
        def create(self, **kw):
            calls.append(kw["messages"][0]["content"])
            return _Resp("NOT FOUND: nothing." if len(calls) == 1
                         else "FOUND: 2.5% per ird.govt.nz.")

    class _Client:
        messages = _Msgs()

        def with_options(self, **k):
            return self

    monkeypatch.setitem(__import__("sys").modules, "anthropic",
                        type("m", (), {"Anthropic": lambda **k: _Client()}))
    got = research._one("bright-line rate", "k", "m", None)
    assert len(calls) == 2, f"{len(calls)} searches"
    assert got.ok is True and got.tried_twice is True
    assert "DIFFERENT wording" in calls[1]


def test_a_finding_on_the_first_go_is_not_searched_twice(monkeypatch):
    calls: list[str] = []

    class _Resp:
        content = [type("B", (), {"type": "text", "text": "FOUND: 2.5%."})()]

    class _Msgs:
        def create(self, **kw):
            calls.append(1)
            return _Resp()

    class _Client:
        messages = _Msgs()

        def with_options(self, **k):
            return self

    monkeypatch.setitem(__import__("sys").modules, "anthropic",
                        type("m", (), {"Anthropic": lambda **k: _Client()}))
    got = research._one("q", "k", "m", None)
    assert len(calls) == 1 and got.tried_twice is False


def test_it_stops_at_two_and_says_it_tried(monkeypatch):
    """A researcher allowed to keep trying will keep trying, and the slowest one
    already sets the clock for all four."""
    calls: list[str] = []

    class _Resp:
        content = [type("B", (), {"type": "text", "text": "NOT FOUND: no."})()]

    class _Msgs:
        def create(self, **kw):
            calls.append(1)
            return _Resp()

    class _Client:
        messages = _Msgs()

        def with_options(self, **k):
            return self

    monkeypatch.setitem(__import__("sys").modules, "anthropic",
                        type("m", (), {"Anthropic": lambda **k: _Client()}))
    got = research._one("q", "k", "m", None)
    assert len(calls) == 2, f"{len(calls)} searches — it kept going"
    assert got.ok is False and got.tried_twice is True

    monkeypatch.setattr(research, "_one", lambda q, *a, **k: got)
    assert "searched twice" in research.research(
        ["bright-line rate"], api_key="k", model="m")
