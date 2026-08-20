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
REQUEST_TIMEOUT = 90.0      # seconds, per model call
DEADLINE = 150.0            # seconds, whole question

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
) -> Result:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT,
                                 max_retries=1)
    started = time.monotonic()
    tools = [{
        "name": t["name"], "description": t["description"],
        "input_schema": t["parameters"],
    } for t in specs]

    convo = list(messages)
    used: list[str] = []
    queries: list[str] = []

    for i in range(MAX_ITERATIONS):
        if time.monotonic() - started > DEADLINE:
            return Result(text=_out_of_time(used), tools_used=used,
                          iterations=i, queries=queries)
        try:
            resp = client.messages.create(
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
            return Result(text=text, tools_used=used, iterations=i + 1, queries=queries)

        convo.append({"role": "assistant", "content": resp.content})
        results = []
        for call in calls:
            used.append(call.name)
            if call.name == "query_data":
                queries.append(str(call.input.get("sql", "")))
            results.append({
                "type": "tool_result", "tool_use_id": call.id,
                "content": dispatch(call.name, call.input),
            })
        convo.append({"role": "user", "content": results})

    return Result(text=_ran_out(), tools_used=used, iterations=MAX_ITERATIONS,
                  queries=queries)


# --- OpenAI ----------------------------------------------------------------

def _run_openai(
    api_key: str, system: str, messages: list[dict], specs: list[dict],
    dispatch: Callable[[str, dict], str],
) -> Result:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=1)
    started = time.monotonic()
    tools = [{"type": "function", "function": {
        "name": t["name"], "description": t["description"],
        "parameters": t["parameters"],
    }} for t in specs]

    convo: list[dict[str, Any]] = [{"role": "system", "content": system}]
    convo += [{"role": m["role"], "content": m["content"]} for m in messages]
    used: list[str] = []
    queries: list[str] = []

    for i in range(MAX_ITERATIONS):
        if time.monotonic() - started > DEADLINE:
            return Result(text=_out_of_time(used), tools_used=used,
                          iterations=i, queries=queries)
        try:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL, messages=convo, tools=tools,
                max_completion_tokens=MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001
            raise _friendly(exc) from exc

        msg = resp.choices[0].message
        if not msg.tool_calls:
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
            convo.append({
                "role": "tool", "tool_call_id": call.id,
                "content": dispatch(call.function.name, args),
            })

    return Result(text=_ran_out(), tools_used=used, iterations=MAX_ITERATIONS,
                  queries=queries)


def _ran_out() -> str:
    return ("I couldn't finish that one — it needed more steps than allowed. "
            "Try asking something narrower.")


def _out_of_time(used: list[str]) -> str:
    """Out of wall-clock rather than out of steps.

    Says so plainly instead of dropping the connection. A question that reaches
    here was being worked on when the clock ran out, so it names what it got
    through — that is usually enough to tell whether narrowing the question or
    asking it a different way is the right next move.
    """
    ran = ", ".join(sorted(set(used))) if used else "no tools"
    return ("That question took longer than I'm allowed to spend on one answer, "
            f"so I stopped part-way (I had run: {ran}). Ask it in smaller "
            "pieces — one suburb, or one property at a time — and I'll get there.")


RUNNERS = {"anthropic": _run_anthropic, "openai": _run_openai}


def run(provider: str, api_key: str, system: str, messages: list[dict],
        specs: list[dict], dispatch: Callable[[str, dict], str]) -> Result:
    runner = RUNNERS.get(provider)
    if runner is None:
        raise ProviderError(f"Unknown provider '{provider}'.")
    try:
        return runner(api_key, system, messages, specs, dispatch)
    except ImportError as exc:
        # The SDK is in requirements.txt, so this means a deploy installed
        # something else. Uncaught it is a 500 with a traceback; as a
        # ProviderError the admin sees which package is missing.
        raise ProviderError(
            f"The {provider} client library is not installed on the server ({exc})."
        ) from exc
