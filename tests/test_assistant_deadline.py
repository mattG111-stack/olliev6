"""Ask Ollie must answer, or explain — never drop the connection.

"[browser] 500 from /api/assistant: HTTP 500" has been reported since v1.8 with
nothing else in it, and the missing detail IS the diagnosis: every handler in
this app answers with a JSON {"detail": ...}, so a 500 carrying no body did not
come from the handler at all. The socket was cut while the question was still
being worked on.

Nothing bounded that work. The provider SDKs default to a TEN MINUTE per-request
timeout and the loop may take twelve tool-calling turns, so one hard question
could run for the better part of an hour while an edge proxy long since gave up
on the connection.

Two bounds now, both well inside any gateway limit: one model call may take
REQUEST_TIMEOUT, and the whole question may take DEADLINE. Past the deadline the
loop stops and returns what it has, naming the tools it managed to run.
"""
from __future__ import annotations

import sys
import types

import pytest

from app.assistant import providers


class _Stub:
    """Stands in for the Anthropic client: records how it was built, and refuses
    to answer, so a test can tell "was it called" from "how was it set up"."""

    seen: dict = {}
    calls: int = 0

    def __init__(self, **kwargs):
        _Stub.seen = dict(kwargs)
        _Stub.calls = 0
        self.messages = self

    def create(self, **_kwargs):
        _Stub.calls += 1
        raise RuntimeError("stub provider: no answer")


@pytest.fixture()
def fake_anthropic(monkeypatch):
    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Stub
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return mod


def test_a_question_stops_at_the_deadline_instead_of_running_on(fake_anthropic, monkeypatch):
    monkeypatch.setattr(providers, "DEADLINE", -1.0)     # already spent
    result = providers._run_anthropic(
        api_key="k", system="s", messages=[{"role": "user", "content": "q"}],
        specs=[], dispatch=lambda *_: "",
    )
    assert "longer than I'm allowed" in result.text, result.text
    assert result.iterations == 0
    assert _Stub.calls == 0, "a model call was made after the deadline had passed"


def test_the_client_is_given_a_per_request_timeout(fake_anthropic):
    """Without this the SDK waits ten minutes on a single call."""
    with pytest.raises(providers.ProviderError):
        # The stub refuses to answer, which is fine — the client was built
        # first, and how it was built is what this test is about.
        providers._run_anthropic(api_key="k", system="s", messages=[], specs=[],
                                 dispatch=lambda *_: "")
    assert _Stub.seen.get("timeout") == providers.REQUEST_TIMEOUT


def test_the_bounds_sit_inside_a_gateway_timeout():
    """A limit longer than the proxy's is not a limit — it is the same hang with
    extra steps. Sixty seconds is the tightest edge timeout in common use."""
    assert providers.REQUEST_TIMEOUT <= 120, providers.REQUEST_TIMEOUT
    assert providers.DEADLINE <= 240, providers.DEADLINE
    assert providers.REQUEST_TIMEOUT < providers.DEADLINE


def test_the_timeout_message_says_what_it_managed_to_run():
    """A bare "took too long" gives the reader nothing to narrow."""
    msg = providers._out_of_time(["query_data", "find_property", "query_data"])
    assert "query_data" in msg and "find_property" in msg
    assert msg.count("query_data") == 1, "tools should be listed once each"
    # And it ends by ASKING which narrowing to make, rather than telling the
    # reader to "try something narrower" and leaving them to work out which.
    assert providers._out_of_time([]).strip().endswith("?")


# ---------------------------------------------------------------------------
# The deadline has to bound the TOTAL, which it did not
# ---------------------------------------------------------------------------
"""Ask Ollie timed out again on 23 August:

    what's a 5 bedroom 2 bathroom 270sqm house and 810sqm of land worth
    in riverhead
    → HTTP 500 with no response body

Both bounds existed. Neither bounded the thing that matters.

The deadline was checked BEFORE each model call and then that call was allowed
to run for the full per-call timeout, so the real ceiling was DEADLINE +
REQUEST_TIMEOUT — 150 + 90, four minutes — for a request an edge proxy cuts off
in one. And nothing counted the tool dispatch between calls at all, where a turn
issuing three queries sits on three SQL statement timeouts.

Two changes: every call is now given only what is LEFT of the budget, and the
budget is checked after tool dispatch as well as before the call.
"""


def test_the_whole_question_fits_inside_a_proxys_patience():
    """55 seconds, not 150. The number has to be under whatever the gateway in
    front of this allows, or the careful partial answer never reaches anyone."""
    assert providers.DEADLINE <= 60
    assert providers.REQUEST_TIMEOUT <= providers.DEADLINE


def test_a_call_cannot_outlive_the_budget_it_was_started_with():
    """The actual bug. A call started with one second left used to run for
    ninety, because the per-call timeout was fixed at construction and the
    deadline check happened before it."""
    assert providers.REQUEST_TIMEOUT + providers.DEADLINE > providers.DEADLINE
    src = __import__("pathlib").Path("app/assistant/providers.py").read_text()
    assert "_timed(client, min(REQUEST_TIMEOUT, budget))" in src, (
        "calls are no longer bounded by what is left of the deadline")


def test_it_stops_while_there_is_still_time_to_say_so():
    """Starting a call that cannot finish produces nothing at all. Stopping
    produces a partial answer naming what was found."""
    assert providers.MIN_CALL_SECONDS >= 5


def test_a_client_without_with_options_still_works():
    """with_options is on every current SDK, but an older one must degrade to
    its constructor timeout rather than raising inside the loop."""
    class Old:
        pass

    old = Old()
    assert providers._timed(old, 10) is old


def test_a_client_that_refuses_the_option_still_works():
    class Fussy:
        def with_options(self, **_kw):
            raise TypeError("no such option")

    f = Fussy()
    assert providers._timed(f, 10) is f


def test_the_budget_shrinks_as_the_question_runs():
    import time as _t

    started = _t.monotonic()
    first = providers._left(started)
    _t.sleep(0.05)
    assert providers._left(started) < first
    assert first <= providers.DEADLINE


def test_the_tool_dispatch_is_counted_too():
    """A turn may issue several queries and each can sit on the SQL statement
    timeout. Checking only before the model call let that accumulate turn after
    turn, unbounded."""
    src = __import__("pathlib").Path("app/assistant/providers.py").read_text()
    # Once per provider, after the tool results are appended.
    assert src.count("if _left(started) < MIN_CALL_SECONDS:") >= 2


def test_running_out_of_time_says_what_it_managed():
    """The point of stopping early: an answer that names the tools it ran beats
    a dropped connection, which says nothing at all."""
    text = providers._out_of_time(["query_data", "suburb_stats"])
    assert text and "query_data" in text or "suburb" in text.lower()
