"""If a listing is not a deal, the app has to be able to say why.

    "You should be able to tell now how have we lost so many deals"

$23.5M of margin across 236 listings sits behind that question, and the only
thing that makes it answerable is deal_block_reason — the sentence written down
at the moment the deal signal is dropped. A drop with no sentence is worse than
no column at all: the listing shows up in "why no deal" with the answer blank,
which reads as a bug in the column rather than as the missing reason it is.

One such drop was live. A listing valued from matched sold comps — the path for
a house whose council record is unusable but whose neighbours are selling — had
its deal signal removed with a good reason stated only in a code comment. The
reason never reached the row.

So this pins the rule rather than that one case: every place the pricing run
drops the deal signal must set a reason in the same breath.
"""
from __future__ import annotations

import inspect
import os
import re

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")

from app.pricing import pipeline  # noqa: E402


def _run_source() -> list[str]:
    return inspect.getsource(pipeline.run).splitlines()


def test_every_place_the_deal_signal_is_dropped_says_why():
    """Reads the run itself rather than trusting a list kept by hand.

    A `deal_value = None` has to have a `deal_block = ...` within a few lines of
    it — before or after, since the code does both. The one exception is the
    line that DERIVES deal_value from deal_block, which is the rule itself.
    """
    lines = _run_source()
    missing = []

    for i, line in enumerate(lines):
        s = line.strip()
        if "deal_value = None" not in s:
            continue
        # `deal_value = None if deal_block else ollie_value` is the rule, not a drop.
        if "if deal_block" in s:
            continue
        window = "\n".join(lines[max(0, i - 6):i + 8])
        if not re.search(r"deal_block = (?!None)", window):
            missing.append((i + 1, s))

    assert not missing, (
        "the deal signal is dropped with no reason recorded at:\n"
        + "\n".join(f"  line {n} of pipeline.run: {s}" for n, s in missing)
        + "\nA listing that is not a deal has to be able to say why."
    )


def test_the_check_would_notice_a_drop_with_no_reason():
    """Guard on the guard. A test that reads source is only worth having if it
    fails when the thing it describes is untrue — this proves the window search
    is not simply matching everything."""
    lines = ["        deal_value = None", "        x = 1", "        y = 2"]
    window = "\n".join(lines)
    assert not re.search(r"deal_block = (?!None)", window)


def test_a_reason_is_only_published_when_there_is_no_deal():
    """The other direction. A listing that IS a deal must not carry a leftover
    reason from a guard that ran and then got overruled."""
    src = inspect.getsource(pipeline.run)
    assert '"deal_block_reason": (deal_block if deal_value is None else None)' in src
