"""Run the same tool-calling loop against Anthropic or OpenAI.

The tools, the schema and the grounding rules are identical either way — only
the wire format for declaring tools and reading tool calls differs. Keeping
that difference in one file means adding a third provider later touches nothing
else, and the safety rules can't drift between vendors.

Both loops are written by hand rather than using each SDK's runner helper: the
two runners have different iteration semantics, and one explicit loop that
behaves identically on both is easier to reason about than two that almost do.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_ITERATIONS = 12
MAX_TOKENS = 8000

# Nothing bounded how long a question could take. The SDKs default to a ten
# MINUTE per-request timeout, and twelve tool iterations can chain several of
# them, so a hard question simply ran until something upstream gave up on the
# connection. What the browser then sees is not this service's error at all: the
# socket is cut mid-response, so there is no JSON body, no `detail`, and the
# report reads "500 from /api/assistant: HTTP 500" with nothing else in it —
# which is why this has been reported for months and never diagnosed.
#
# Two bounds now. One request may take REQUEST_TIMEOUT; the whole question may
# take DEADLINE, after which the loop stops and returns what it has. Both sit
# well inside the minute-scale limits an edge proxy imposes, so a slow question
# comes back as an answer or an explained error, never as a dropped connection.
REQUEST_TIMEOUT = 40.0      # seconds, ceiling for ONE model call
DEADLINE = 55.0             # seconds, the WHOLE question
# Below this there is no point starting another model call — it cannot finish
# inside what is left, and a call that is cut off produces nothing at all where
# stopping produces a partial answer naming what was found.
MIN_CALL_SECONDS = 10.0

# --- and when nothing is waiting -------------------------------------------
# The two numbers above exist for ONE reason: a synchronous request has to beat
# the proxy in front of it, so a hard question is cut short at 55 seconds and
# answered part-way rather than dropped. That is a compromise forced by the
# transport, not by the question.
#
#     "a user asks a question then ollie does not time out till it answers it"
#
# Asked through the job endpoint there is no proxy to beat, so `deadline=None`
# and the loop runs until the model is finished. A single call still gets a
# ceiling — a socket that has silently died must not hang the thread for ever —
# but a generous one, because taking six minutes over a hard question is a
# feature here rather than a fault.
CALL_TIMEOUT_UNBOUNDED = 300.0    # seconds, ceiling for ONE call when untimed
JOB_MAX_ITERATIONS = 24           # more room to work when time is not the bound

_FOREVER = float("inf")

# `None` is a meaningful value here — it means "no deadline at all" — so it
# cannot double as "not specified". A default of `deadline=DEADLINE` would not
# do either: a default expression is evaluated once, when the function is
# DEFINED, so the module's DEADLINE would be frozen at import and changing it
# afterwards would silently do nothing. This sentinel keeps the module
# attribute the authority, read fresh on every call.
_UNSET: Any = object()


def _budget(deadline: Any) -> float | None:
    return DEADLINE if deadline is _UNSET else deadline


def _left(started: float, deadline: Any = _UNSET) -> float:
    """Seconds remaining in the whole question's budget.

    `None` means there is no budget: infinity is returned, so every
    `_left(...) < MIN_CALL_SECONDS` check below is simply false and the loop
    stops on having an answer rather than on a clock. One rule, both providers,
    rather than an `if untimed` beside each of the four checks — which is how
    the two loops would drift apart.
    """
    limit = _budget(deadline)
    if limit is None:
        return _FOREVER
    return limit - (time.monotonic() - started)


def _note(on_step: Callable[[str, str], None] | None, kind: str, detail: str) -> None:
    """Tell the caller what is happening, if it asked to be told.

    Progress reporting must never be able to break an answer: this is called
    from inside the loop that is doing the real work, and a callback that
    writes to a database can fail for reasons that have nothing to do with the
    question. Swallow it.
    """
    if on_step is None:
        return
    try:
        on_step(kind, detail)
    except Exception:                             # noqa: BLE001
        pass


def _timed(client, seconds: float):
    """The same client, bounded to `seconds` for the next call.

    Both SDKs take a timeout at construction, which is a per-CALL bound and
    therefore useless as a total: the loop checked the deadline BEFORE each
    call and then let that call run for the full per-call timeout, so the real
    ceiling was DEADLINE + REQUEST_TIMEOUT and not DEADLINE. At the old numbers
    that was 150 + 90 = four minutes for something the proxy cuts off in one.

    with_options exists on every current anthropic and openai SDK; an older one
    keeps its constructor timeout rather than breaking.
    """
    opts = getattr(client, "with_options", None)
    if opts is None:
        return client
    try:
        return opts(timeout=max(1.0, seconds))
    except Exception:                             # noqa: BLE001
        return client

ANTHROPIC_MODEL = "claude-opus-4-8"
OPENAI_MODEL = "gpt-5"


@dataclass
class Result:
    text: str
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    queries: list[str] = field(default_factory=list)


class ProviderError(RuntimeError):
    """Anything the user needs to act on — bad key, no credit, wrong provider."""


def _friendly(exc: Exception) -> ProviderError:
    msg = str(exc)
    low = msg.lower()
    if "authentication" in low or "invalid_api_key" in low or "401" in low:
        return ProviderError("That API key was rejected. Check it in Settings.")
    if "insufficient_quota" in low or "credit balance" in low or "billing" in low:
        return ProviderError("The provider reports no available credit on that key.")
    if "rate limit" in low or "429" in low:
        return ProviderError("Rate limited by the provider. Try again shortly.")
    if "model" in low and "not found" in low:
        return ProviderError("That model isn't available on your account's plan.")
    return ProviderError(f"{type(exc).__name__}: {msg[:300]}")


# --- Anthropic -------------------------------------------------------------

def _run_anthropic(
    api_key: str, system: str, messages: list[dict], specs: list[dict],
    dispatch: Callable[[str, dict], str],
    *, deadline: Any = _UNSET, max_iterations: int = MAX_ITERATIONS,
    on_step: Callable[[str, str], None] | None = None,
) -> Result:
    import anthropic

    per_call = CALL_TIMEOUT_UNBOUNDED if _budget(deadline) is None else REQUEST_TIMEOUT
    client = anthropic.Anthropic(api_key=api_key, timeout=per_call, max_retries=1)
    started = time.monotonic()
    tools = [{
        "name": t["name"], "description": t["description"],
        "input_schema": t["parameters"],
    } for t in specs]

    convo = list(messages)
    used: list[str] = []
    queries: list[str] = []

    for i in range(max_iterations):
        # Stop while there is still time to say so. The old check let a call
        # start with a second left and run for another ninety. With no deadline
        # `budget` is infinite and this never fires.
        budget = _left(started, deadline)
        if budget < MIN_CALL_SECONDS:
            return Result(text=_out_of_time(used), tools_used=used,
                          iterations=i, queries=queries)
        _note(on_step, "thinking", "")
        try:
            resp = _timed(client, min(per_call, budget)).messages.create(
                model=ANTHROPIC_MODEL, max_tokens=MAX_TOKENS, system=system,
                thinking={"type": "adaptive"},
                output_config={"effort": "xhigh"},
                tools=tools, messages=convo,
            )
        except Exception as exc:  # noqa: BLE001
            raise _friendly(exc) from exc

        if resp.stop_reason == "refusal":
            return Result(text="The provider declined to answer that.", iterations=i + 1)

        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            _note(on_step, "writing", "")
            return Result(text=text, tools_used=used, iterations=i + 1, queries=queries)

        convo.append({"role": "assistant", "content": resp.content})
        results = []
        for call in calls:
            used.append(call.name)
            if call.name == "query_data":
                queries.append(str(call.input.get("sql", "")))
            _note(on_step, "tool", call.name)
            results.append({
                "type": "tool_result", "tool_use_id": call.id,
                "content": dispatch(call.name, call.input),
            })
        convo.append({"role": "user", "content": results})
        # Tool dispatch is not free either — each query_data can sit on the
        # SQL statement timeout, and a turn may issue several. Checking here as
        # well as at the top of the loop bounds the overrun to ONE round of
        # tools rather than letting it accumulate turn after turn.
        if _left(started, deadline) < MIN_CALL_SECONDS:
            return Result(text=_out_of_time(used), tools_used=used,
                          iterations=i + 1, queries=queries)

    return Result(text=_ran_out(used), tools_used=used, iterations=max_iterations,
                  queries=queries)


# --- OpenAI ----------------------------------------------------------------

def _run_openai(
    api_key: str, system: str, messages: list[dict], specs: list[dict],
    dispatch: Callable[[str, dict], str],
    *, deadline: Any = _UNSET, max_iterations: int = MAX_ITERATIONS,
    on_step: Callable[[str, str], None] | None = None,
) -> Result:
    from openai import OpenAI

    per_call = CALL_TIMEOUT_UNBOUNDED if _budget(deadline) is None else REQUEST_TIMEOUT
    client = OpenAI(api_key=api_key, timeout=per_call, max_retries=1)
    started = time.monotonic()
    tools = [{"type": "function", "function": {
        "name": t["name"], "description": t["description"],
        "parameters": t["parameters"],
    }} for t in specs]

    convo: list[dict[str, Any]] = [{"role": "system", "content": system}]
    convo += [{"role": m["role"], "content": m["content"]} for m in messages]
    used: list[str] = []
    queries: list[str] = []

    for i in range(max_iterations):
        budget = _left(started, deadline)
        if budget < MIN_CALL_SECONDS:
            return Result(text=_out_of_time(used), tools_used=used,
                          iterations=i, queries=queries)
        _note(on_step, "thinking", "")
        try:
            resp = _timed(client, min(per_call, budget)).chat.completions.create(
                model=OPENAI_MODEL, messages=convo, tools=tools,
                max_completion_tokens=MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001
            raise _friendly(exc) from exc

        msg = resp.choices[0].message
        if not msg.tool_calls:
            _note(on_step, "writing", "")
            return Result(text=(msg.content or "").strip(), tools_used=used,
                          iterations=i + 1, queries=queries)

        convo.append({
            "role": "assistant", "content": msg.content,
            "tool_calls": [{
                "id": c.id, "type": "function",
                "function": {"name": c.function.name, "arguments": c.function.arguments},
            } for c in msg.tool_calls],
        })
        for call in msg.tool_calls:
            used.append(call.function.name)
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if call.function.name == "query_data":
                queries.append(str(args.get("sql", "")))
            _note(on_step, "tool", call.function.name)
            convo.append({
                "role": "tool", "tool_call_id": call.id,
                "content": dispatch(call.function.name, args),
            })
        # Same bound as the other provider: one round of tools may overrun, not
        # every round.
        if _left(started, deadline) < MIN_CALL_SECONDS:
            return Result(text=_out_of_time(used), tools_used=used,
                          iterations=i + 1, queries=queries)

    return Result(text=_ran_out(used), tools_used=used, iterations=max_iterations,
                  queries=queries)


def _ran_out(used: list[str] | None = None) -> str:
    return ("I couldn't finish that one — it took more steps than I'm allowed. "
            + _narrow(used or []))


def _out_of_time(used: list[str]) -> str:
    """Out of wall-clock rather than out of steps.

    Says so plainly instead of dropping the connection. A question that reaches
    here was being worked on when the clock ran out, so it names what it got
    through — that is usually enough to tell whether narrowing the question or
    asking it a different way is the right next move.
    """
    ran = ", ".join(sorted(set(used))) if used else "no tools"
    return ("That question took longer than I'm allowed to spend on one answer, "
            f"so I stopped part-way (I had run: {ran}). " + _narrow(used))


def _narrow(used: list[str]) -> str:
    """End the two give-up paths on a question rather than an apology.

    Both used to close with "try asking something narrower", which puts the
    work of guessing what went wrong back on the person who asked. What
    actually ran is known here, and it says which narrowing is the useful
    one: a run that never got past ad-hoc SQL was almost certainly a question
    with no tool behind it, and one that was mid-search wanted a smaller area.
    """
    tools = set(used)
    if tools and tools <= {"query_data", "distinct_values"}:
        return ("It was open-ended, so I was working it out query by query. "
                "Give me the single number you're after and I'll go straight "
                "at it — a median, a count, or a ranking?")
    if "search_listings" in tools:
        return ("Narrowing the area is usually all it takes. Which one shall I "
                "hold it to — a suburb, or a district?")
    return ("Tell me the part that matters most and I'll answer that properly. "
            "A particular suburb, a price bracket, or one property?")


RUNNERS = {"anthropic": _run_anthropic, "openai": _run_openai}


def run(provider: str, api_key: str, system: str, messages: list[dict],
        specs: list[dict], dispatch: Callable[[str, dict], str],
        *, deadline: Any = _UNSET,
        max_iterations: int = MAX_ITERATIONS,
        on_step: Callable[[str, str], None] | None = None) -> Result:
    """Answer one question.

    `deadline=None` means take as long as it takes — only correct when nothing
    upstream is waiting on the answer, i.e. from the job worker. A synchronous
    request must keep the default, or it goes back to being cut off mid-flight
    and reported as a bodyless 500.
    """
    runner = RUNNERS.get(provider)
    if runner is None:
        raise ProviderError(f"Unknown provider '{provider}'.")
    try:
        return runner(api_key, system, messages, specs, dispatch,
                      deadline=deadline, max_iterations=max_iterations,
                      on_step=on_step)
    except ImportError as exc:
        # The SDK is in requirements.txt, so this means a deploy installed
        # something else. Uncaught it is a 500 with a traceback; as a
        # ProviderError the admin sees which package is missing.
        raise ProviderError(
            f"The {provider} client library is not installed on the server ({exc})."
        ) from exc
