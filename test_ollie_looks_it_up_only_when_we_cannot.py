"""When Ollie may go outside our data, and the far more important when it may not.

    "if ollie cant answer a question via our data it then needs to look on the
     internet for the relvent data to answer the question but only if it can
     answer the question from our data"

There is a real gap. Mortgage rates, LVR rules, a plan change, a consent fee,
what a zone rule actually says — all of it bears on a property decision, none of
it is in the database, and today the answer is "I can't help with that".

The risk is not the search. It is that "cannot answer" is two situations wearing
the same words:

    WE HOLD NOTHING OF THAT KIND. Nobody has loaded mortgage rates and nobody
    will. Looking it up is the right move.

    WE HOLD EXACTLY THAT AND THE QUESTION MISSED IT. The suburb was spelled
    differently, the filters were too tight, the sold rows carry no listing
    date. Ollie already handles this: a tool returns a CANNOT ANSWER YET block
    naming the one missing thing, and the answer is to ask for it.

Searching the web in the second case is worse than staying silent. It would
answer a question about OUR market off somebody else's website — a Grey Lynn
median from a portal rather than out of the sold file every number here is
measured against — and it would read like an answer. The whole product is a
promise that a figure came from data we hold.

So these tests are mostly negative, and that is the point.
"""
from __future__ import annotations

import pytest

from app.assistant import websearch
from app.assistant.agent import SYSTEM


# ---- it exists at all --------------------------------------------------------
def test_the_tool_is_declared_and_bounded():
    block = websearch.tool_block()
    assert block["type"] == websearch.TOOL_TYPE
    assert block["name"] == "web_search"
    assert 1 <= block["max_uses"] <= 5, (
        "the question has already failed against our data by this point — a "
        "model given ten searches will use ten")


def test_it_is_restricted_to_sources_worth_quoting():
    """An open search on a property question returns portals, blogs and
    agencies: all have numbers, none can be stood behind."""
    allowed = websearch.tool_block()["allowed_domains"]
    assert allowed, "an unrestricted web search is not this feature"
    assert "rbnz.govt.nz" in allowed          # the body that sets the rules
    assert "aucklandcouncil.govt.nz" in allowed


def test_the_list_can_actually_answer_the_two_commonest_gaps():
    """Safe and useless is not the goal. Rates and published market statistics
    are what people ask that our data does not hold, so the sources of record
    for both have to be reachable."""
    allowed = websearch.tool_block()["allowed_domains"]
    assert "interest.co.nz" in allowed        # the published mortgage rate table
    assert "reinz.co.nz" in allowed           # the national house price index


def test_no_property_portal_is_an_allowed_source():
    """Their numbers compete with ours, and "compete" is the polite word for
    "would be quoted interchangeably with ours"."""
    allowed = " ".join(websearch.tool_block()["allowed_domains"]).lower()
    for portal in ("trademe", "oneroof", "realestate.co.nz", "homes.co.nz",
                   "qv.co.nz", "corelogic"):
        assert portal not in allowed, f"{portal} is quotable as if it were ours"


# ---- the rule that matters ---------------------------------------------------
def test_a_gap_block_says_whether_looking_it_up_would_help():
    """THE ONE THAT MATTERS, and the distinction the first version got wrong.

    "if it cant find the answer in the data it look on the internet" is right,
    and most gaps here are not that. They are gaps the USER closes: a suburb
    spelled differently, a bedroom count nobody gave. No search fixes a
    spelling — it answers a different question confidently.

    So the block itself decides, and the model is told to obey it rather than
    judge for itself.
    """
    from app.assistant.tools import _gap

    theirs = _gap(need="the right spelling", ask="Did you mean Riverhead?")
    assert "Looking it up: NO" in theirs

    ours = _gap(need="a listing date", ask="Want the published figure?",
                outside_ok=True)
    assert "Looking it up: ALLOWED" in ours

    rules = websearch.SYSTEM_RULES
    assert "Looking it up: NO" in rules and "Looking it up: ALLOWED" in rules
    assert "do not decide for yourself" in rules
    assert "Looking it up: ALLOWED" in SYSTEM, (
        "the guard is not in the prompt the model actually receives")


def test_a_spelling_gap_is_never_sent_to_the_web():
    """A misspelled suburb is the commonest gap of all, and the one where a
    search does most damage: it would answer about a real place that is not the
    place the person meant."""
    from app.assistant.tools import _gap

    assert "Looking it up: NO" in _gap(
        need="the right spelling for 'Riverhed'",
        ask="Did you mean Riverhead?")


def test_the_days_to_sell_dead_end_is_allowed_to_look_it_up():
    """The case the instruction was actually about. The sold file carries no
    listing date for most of its rows, so no rephrasing produces an answer, and
    the person asking was left with nothing."""
    import inspect

    from app.assistant import tools

    src = inspect.getsource(tools.suburb_days_to_sell)
    assert "outside_ok=True" in src, (
        "the one dead end our data can never close is still refusing to look")


def test_the_prompt_forbids_a_figure_about_our_own_market():
    """Grounding does not bend for a good source. Valuations, medians, sale
    prices and listing counts come from tools or they do not get said."""
    # Flattened: the prompt is wrapped for reading, and a rule that straddles a
    # newline is not a missing rule.
    rules = " ".join(websearch.SYSTEM_RULES.split())
    for word in ("valuation", "median", "sale price", "yield"):
        assert word in rules, f"the prompt does not rule out a web {word}"
    assert "what the RULES are, never what a property IS worth" in rules


def test_the_prompt_forbids_putting_a_customer_into_a_search():
    assert "customer's name, email, or saved search" in websearch.SYSTEM_RULES


def test_the_prompt_says_to_name_the_source():
    """A figure from outside must not be mistakable for one of ours. This is
    honesty about the ANSWER, which is a different thing from explaining how the
    product works."""
    # Flattened: the prompt is wrapped for reading, and a phrase that happens
    # to straddle a newline is not a missing rule.
    flat = " ".join(websearch.SYSTEM_RULES.split())
    assert "outside our data" in flat
    assert "name the source" in flat


def test_the_grounding_rule_still_comes_first():
    """The web rules sit below the grounding block, not in place of it."""
    assert SYSTEM.index("Every number you state MUST come from a tool call") \
        < SYSTEM.index("LOOKING SOMETHING UP OUTSIDE OUR DATA")


# ---- a wrong guess about the tool must not take the assistant down -----------
def test_a_refusal_naming_the_tool_is_recognised():
    """This was written on a machine that cannot reach the API to confirm the
    tool block's shape. A rejected tool that failed the whole request would take
    down every question rather than one feature."""
    assert websearch.is_tool_rejection(
        Exception("400 invalid_request_error: tools.12: unexpected type "
                  "web_search_20260209"))
    assert websearch.is_tool_rejection(
        Exception("Error code: 400 - allowed_domains is not supported"))


def test_other_failures_are_not_swallowed_as_a_tool_problem():
    """A key problem or a bad model name must surface as itself. Retrying those
    without the search tool would hide the real fault behind a feature nobody
    asked about."""
    for other in ("401 authentication_error: invalid x-api-key",
                  "429 rate_limit_error",
                  "404 model not found: claude-nonexistent",
                  "500 internal server error"):
        assert not websearch.is_tool_rejection(Exception(other)), other


def test_the_runner_drops_the_tool_and_retries_once(monkeypatch):
    """The whole point of recognising it: lose the lookup, keep the answer."""
    from app.assistant import providers

    seen: list[bool] = []

    class _Msgs:
        def create(self, **kw):
            has_web = any(t.get("type") == websearch.TOOL_TYPE
                          for t in kw["tools"] if isinstance(t, dict))
            seen.append(has_web)
            if has_web:
                raise Exception("400 invalid_request_error: tools.9: "
                                "unexpected type web_search_20260209")

            class _Block:
                type = "text"
                text = "Auckland's median asking is $949k."

            class _Resp:
                stop_reason = "end_turn"
                content = [_Block()]
            return _Resp()

    class _Client:
        messages = _Msgs()

        def with_options(self, **kw):
            return self

    monkeypatch.setitem(__import__("sys").modules, "anthropic",
                        type("m", (), {"Anthropic": lambda **kw: _Client()}))

    got = providers._run_anthropic(
        api_key="k", system="s", messages=[{"role": "user", "content": "q"}],
        specs=[], dispatch=lambda n, a: "", deadline=None, max_iterations=3)

    assert seen == [True, False], f"retry pattern was {seen}"
    assert "949k" in got.text, "the answer was lost with the tool"
