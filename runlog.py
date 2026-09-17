"""What happened to this load, and why.

Four stages run over an upload — load, enrich, price, publish — and each one
makes decisions that change what a customer eventually sees. Rows are rejected.
Addresses are looked up, or refused, or never reached. Valuations take one path
or another. Deals are suppressed. All of that was decided and then lost: to
stdout, or into a counter shown as a bare total, or nowhere. Afterwards the only
way to answer "why did it do that" was to re-read the code and guess at which
branch a particular row took, which is not an answer.

record() writes the decision down as it is made. Two rules keep it useful:

  IN WORDS, NOT IN CODES. "3,100 rows had no council valuation, which every
  valuation method here needs" is an answer. "rejected: no_cv" is a lookup table
  somebody has to already know.

  COUNTS, NOT ROWS. One event per reason, carrying how many listings it covers —
  so a 2,141-row load writes tens of rows, not thousands. The per-listing detail
  already lives on the listing itself (hold_reason, deal_block_reason,
  pricing_path), and the Excel export joins the two together.

Never raises. A log that can break the run it is logging is worse than no log,
so every failure here is swallowed after being printed — the stage carries on.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import RunEvent

# Matches RunEvent.detail's column-free Text, but a sentence that long is a bug
# in the caller rather than something to store.
_MAX_DETAIL = 2000


def record(db: Session, *, stage: str, event: str, detail: str = "",
           batch_id: int | None = None, job_id: int | None = None,
           count: int | None = None, address: str | None = None,
           level: str = "info", commit: bool = True) -> None:
    """Write one decision down. Silent on failure — see the module docstring."""
    try:
        db.add(RunEvent(
            stage=str(stage)[:16], event=str(event)[:48], level=str(level)[:8],
            detail=(detail or "")[:_MAX_DETAIL] or None,
            batch_id=batch_id, job_id=job_id, count=count,
            address=(address or "")[:512] or None,
        ))
        if commit:
            db.commit()
    except Exception as e:                                   # pragma: no cover
        print(f"[runlog] could not record {stage}/{event}: {e}", flush=True)
        try:
            db.rollback()
        except Exception:
            pass


def events(db: Session, batch_id: int | None = None, *,
           limit: int = 2000) -> list[RunEvent]:
    """The log for one load, oldest first — the order things happened in, which
    is the order you need to read them in to see what caused what."""
    q = db.query(RunEvent)
    if batch_id is not None:
        q = q.filter(RunEvent.batch_id == batch_id)
    return list(q.order_by(RunEvent.at.asc(), RunEvent.id.asc()).limit(limit))
