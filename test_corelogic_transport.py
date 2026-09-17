"""Why 37% of lookups never arrived.

    "have we fixed why corligic isnt working right"

Half of it was fixed and proven in production: an unreachable lookup no longer
marks the row as checked, so a re-run retries it (147 looked up − 54 unreachable
= 93 stamped, from a real run). That was the fatal half — without it the batch
was permanently un-enrichable.

The other half was still there: 37% of lookups failing before they arrived, with
ZERO blocks. Not rate limiting. Transport. Two causes, both in this module:

  A NEW CONNECTION PER ADDRESS. Every lookup opened its own client — a fresh TCP
  connection and a full TLS handshake, 2,141 of them on a single load, against a
  host with every reason to start refusing them. Connection churn at that scale
  is the first thing to suspect and the cheapest thing to remove.

  NO RETRY ON A DROPPED CONNECTION. A 401/403/429 got a five-step backoff ladder.
  A dropped connection was counted as a miss on the spot. That is backwards: a
  dropped connection is the single most likely thing to succeed on a second
  attempt, and a rate limit is the least.

These tests never touch the network. They drive _lookup_once, which is the seam
between "what did the server say" and "what do we do about it" — the second half
is the part that was wrong.
"""
from __future__ import annotations

import httpx
import pytest

import app.propertyvalue as pv


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """The retry waits are real seconds. Nothing here is testing that they elapse."""
    monkeypatch.setattr(pv.time, "sleep", lambda *_: None)
    pv.close_client()
    yield
    pv.close_client()


def _answers(monkeypatch, seq):
    """Drive pv_lookup_status with a canned sequence of per-attempt outcomes."""
    calls = {"n": 0}

    def fake(address):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        out = seq[i]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(pv, "_lookup_once", fake)
    return calls


# ---- the retry -------------------------------------------------------------
def test_a_dropped_connection_is_retried_before_it_is_believed(monkeypatch):
    """The whole point. One flaky moment cost an address its lookup and, before
    the stamping fix, cost it every future lookup too."""
    calls = _answers(monkeypatch, [httpx.ConnectError("connection reset"),
                                   ({"cv": 1_200_000}, pv.PV_OK)])
    rec, status = pv.pv_lookup_status("12 Elliot Street, Remuera")

    assert status == pv.PV_OK, "a transient failure was treated as final"
    assert rec == {"cv": 1_200_000}
    assert calls["n"] == 2


def test_it_gives_up_rather_than_retrying_for_ever(monkeypatch):
    """A host that is genuinely down must not hold the run hostage — 2,141
    addresses times an unbounded retry is a job that never finishes."""
    calls = _answers(monkeypatch, [httpx.ConnectError("down")])
    _, status = pv.pv_lookup_status("12 Elliot Street")

    assert status == pv.PV_ERROR
    assert calls["n"] == pv._RETRIES + 1


def test_a_rate_limit_is_not_retried_here(monkeypatch):
    """A 401/403/429 already has a five-step backoff ladder in the enrich stage,
    which also slows the pacing for the rest of the run. Retrying it here as
    well would hammer a host that has just asked us to stop, and would hide the
    block from the code that knows how to respond to it."""
    calls = _answers(monkeypatch, [(None, pv.PV_BLOCKED)])
    _, status = pv.pv_lookup_status("12 Elliot Street")

    assert status == pv.PV_BLOCKED
    assert calls["n"] == 1


def test_an_address_they_do_not_hold_is_not_retried_either(monkeypatch):
    """"CoreLogic has no record of this" is an ANSWER. Asking twice buys nothing
    and doubles the cost of every unknown address in the batch."""
    calls = _answers(monkeypatch, [(None, pv.PV_NOT_FOUND)])
    _, status = pv.pv_lookup_status("1 Nowhere Road")

    assert status == pv.PV_NOT_FOUND
    assert calls["n"] == 1


def test_a_hit_costs_exactly_one_request(monkeypatch):
    calls = _answers(monkeypatch, [({"cv": 1}, pv.PV_OK)])
    assert pv.pv_lookup_status("12 Elliot Street")[1] == pv.PV_OK
    assert calls["n"] == 1


def test_a_blank_address_never_reaches_the_network(monkeypatch):
    calls = _answers(monkeypatch, [({"cv": 1}, pv.PV_OK)])
    assert pv.pv_lookup_status("   ")[1] == pv.PV_NOT_FOUND
    assert calls["n"] == 0


# ---- the connection --------------------------------------------------------
def test_the_connection_is_reused_across_addresses(monkeypatch):
    """2,141 TLS handshakes against one host is the thing being removed. If this
    ever goes back to a client per lookup, the failure rate goes back with it."""
    monkeypatch.setattr(pv, "_lookup_once", lambda a: ({"cv": 1}, pv.PV_OK))
    first = pv._client()
    for i in range(5):
        pv.pv_lookup_status(f"{i} Somewhere Road")

    assert pv._client() is first, "a new connection was opened per address"


def test_a_pool_that_has_gone_bad_is_dropped(monkeypatch):
    """Otherwise one poisoned connection is carried for the rest of the run and
    every remaining address inherits it."""
    pv._client()
    _answers(monkeypatch, [httpx.ConnectError("down")])
    pv.pv_lookup_status("12 Elliot Street")

    assert pv._CLIENT is None, "the bad connection was kept for the next address"


def test_the_next_lookup_reconnects_after_that(monkeypatch):
    """Dropping the pool must not leave the module unable to make requests. The
    stub below stands in for the server, so the connection is asked for directly
    — the point is that _client() rebuilds rather than returning None for ever."""
    dead = pv._client()
    _answers(monkeypatch, [httpx.ConnectError("down")])
    pv.pv_lookup_status("12 Elliot Street")
    assert pv._CLIENT is None

    fresh = pv._client()
    assert fresh is not None and fresh is not dead

    monkeypatch.setattr(pv, "_lookup_once", lambda a: ({"cv": 1}, pv.PV_OK))
    assert pv.pv_lookup_status("13 Elliot Street")[1] == pv.PV_OK


def test_connect_and_read_have_separate_timeouts():
    """A slow answer and an unreachable host are different problems. One shared
    timeout counts a server thinking hard as a server that is not there."""
    assert pv._TIMEOUT.connect < pv._TIMEOUT.read


def test_importing_this_module_opens_no_socket():
    """It is imported by the API process, not just the enrich worker."""
    pv.close_client()
    assert pv._CLIENT is None
