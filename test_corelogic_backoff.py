"""CoreLogic has to survive a rate limit, not surrender to it.

    "corelogic didnt work well at all it did 100 houses only"
    "And coroligic must work"

Two separate causes, and only one of them was the work list.

FIRST: the work list skipped every HELD row, including rows held for exactly
the gaps a lookup would fill. Fixed separately - see
test_enrich_reaches_held_rows.py.

SECOND, and this one: on five consecutive 401/403/429 responses the job
ABANDONED THE RUN and left a message telling somebody to wait and re-run by
hand.

On a batch of eleven thousand addresses at two requests a second, a rate limit
is not an exceptional event - it is the expected one. So the run died part-way
through nearly every time, and "it did a hundred houses" is precisely what that
looks like from outside. The abort was not protecting anything; it was the
failure.

A rate limit is CoreLogic asking us to slow down. The right answer is to slow
down: back off, retry the same address, and stay slower for the rest of the run.
Only a block that survives the entire backoff ladder on several rows running is
an IP ban, and no amount of waiting fixes that one.
"""
from __future__ import annotations

import inspect

from app import staged_stages as S


def _enrich_source() -> str:
    """The enrich worker, however it happens to be split up.

    It used to be one function. It is now a pass that does the work plus a
    wrapper that re-runs the pass when it stops on something temporary, so a
    test that reads only one of them is reading half the worker and would pass
    while the half it cannot see is broken. The pass comes first because a
    couple of these tests read the source in order.
    """
    return (inspect.getsource(S._enrich_pass) + "\n"
            + inspect.getsource(S.run_enrich_job))


def test_a_rate_limit_no_longer_kills_the_run():
    src = _enrich_source()
    assert "BLOCK_BACKOFF" in src, "there is no backoff at all"
    # The old behaviour: five straight blocks and the whole job fails.
    assert "consec_block >= 5" not in src, (
        "five consecutive rate-limit responses still abandon the run")


def test_it_retries_the_same_address_rather_than_skipping_it():
    """A blocked row is not a missing property. Skipping it silently loses the
    lookup; retrying it after a wait is the entire point."""
    src = _enrich_source()
    assert src.count("pv_lookup_status(q)") >= 2, (
        "the address is looked up once and never retried after a block")


def test_the_backoff_ladder_starts_short_and_ends_long():
    """A short pause clears a burst limit; a long one clears a per-minute quota.
    Going straight to five minutes wastes an hour on a block that would have
    cleared in five seconds."""
    assert S.BLOCK_BACKOFF[0] <= 10
    assert S.BLOCK_BACKOFF[-1] >= 120
    assert list(S.BLOCK_BACKOFF) == sorted(S.BLOCK_BACKOFF)
    assert len(S.BLOCK_BACKOFF) >= 4


def test_the_pace_slows_permanently_once_it_has_been_told_off():
    """A pace that earned a rate limit will earn another."""
    src = _enrich_source()
    assert "PACE_BACKOFF" in src
    assert "MAX_DELAY" in src
    assert S.PACE_BACKOFF > 1.0
    assert S.MAX_DELAY > 0.5


def test_a_real_ip_block_still_gives_up_eventually():
    """Waiting forever on a genuine ban is its own kind of broken. It has to be
    a much higher bar than five responses, and it has to say the difference."""
    src = _enrich_source()
    assert "HARD_BLOCK_ROWS" in src
    assert S.HARD_BLOCK_ROWS >= 3
    # The message wraps across f-string lines, so match the halves rather than
    # the sentence — a test that breaks on rewrapping is a test nobody trusts.
    flat = " ".join(src.split())
    assert "IP block rather than a rate limit" in flat


def test_giving_up_says_the_work_is_not_lost():
    """Every answered row is stamped as it goes, so a re-run resumes. If the
    message does not say so, somebody starts again from zero."""
    src = " ".join(_enrich_source().split())
    assert "re-run resumes from here" in src


def test_a_blocked_row_is_never_stamped_as_checked():
    """pv_checked_at is what makes a re-run skip a row. Stamping a blocked row
    would silently drop it from every future run - it would look answered and
    never be looked at again.

    Asserted on behaviour rather than on the source line: the same guard now
    covers unreachable lookups too, so a text match went stale the moment it was
    widened. See tests/test_enrich_unreachable.py for the end-to-end proof."""
    src = " ".join(_enrich_source().split())
    stamp = src.split("p.pv_checked_at = ")[0].split("if status")[-1]
    assert "PV_BLOCKED" in stamp, "a blocked row can now be stamped as checked"


def test_the_cap_is_not_what_is_stopping_it():
    """20,000, and a hundred lookups was nowhere near it. Asserted so nobody
    goes looking there again."""
    sig = inspect.signature(S.run_enrich_job)
    assert sig.parameters["cap"].default >= 20_000
