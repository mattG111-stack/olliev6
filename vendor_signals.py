"""Read a listing for what it tells you about the vendor — and about the house.

Two questions a buyer actually asks, neither of which a price answers:

  How motivated is the vendor? A mortgagee sale, "urgent", "present all offers",
  a price that has been cut, a listing that has sat for months, or a switch to
  by-negotiation are all the same message from different directions: there is
  room to move. Flagged so a buyer can find leverage the portal never labels.

  What is the risk? "reclad", "re-clad", "monolithic" or "plaster clad" on a
  home of the wrong era is the language of a leaky building — the single most
  expensive thing to miss on an Auckland house. Flagged as a caution, never as a
  verdict: it is a prompt to check the weathertightness, not a diagnosis.

Everything is derived from words in the listing plus figures we already hold
(prior asking, days on market, sale method). Pure and side-effect free: give it
the fields, get back tags and plain-English reasons.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# tag -> the phrases that raise it. Word-boundaried so "reclad" does not fire on
# "recladding schedule already completed" — that is handled by the negation below.
_MOTIVATION = {
    "mortgagee": (r"mortgagee", "mortgagee sale"),
    "urgent": (r"urgent|must sell|must be sold|priced to sell|sell(ing)? now|vendor says sell|vendors? want.{0,10}sold", "urgent / must-sell language"),
    "present_all_offers": (r"present all offers|all offers (considered|presented)|bring (all )?offers|offers (invited|encouraged)", "vendor inviting all offers"),
    "estate": (r"deceased estate|estate sale|executor", "deceased estate"),
    "motivated": (r"motivated (vendor|seller)|genuine seller|committed elsewhere|moving overseas|relocating", "vendor stated as motivated"),
    "divorce": (r"separation|divorce", "relationship-split sale"),
}

_RISK = {
    "reclad": (r"re-?\s?clad|reclad(ded|ding)?", "reclad / re-clad mentioned — check weathertightness (leaky-home risk)"),
    "leaky": (r"leaky|weathertight(ness)? (issue|remediation|concern)|water (ingress|tightness)", "weathertightness language — check leaky-home risk"),
    "monolithic": (r"monolithic( clad(ding)?)?|plaster clad|solid plaster", "monolithic/plaster cladding — a leaky-home risk era, check the report"),
    "as_is": (r"as[-\s]?is,?\s?where[-\s]?is|as is where is", "sold 'as is, where is' — expect a defect or consent issue"),
}

# "reclad" is good news when it has already been DONE. These phrases flip a risk
# hit into reassurance, so they suppress the flag rather than raise it.
_RISK_RESOLVED = re.compile(
    r"(fully|completely|recently|already)\s+re-?\s?clad|re-?\s?clad(ded|ding)?\s+(complete|done|in\s+20)|"
    r"new (weathertight|cladding)|weathertight(ness)? (approved|remediated|completed|certificate)",
    re.I)

DOM_STALE_DAYS = 60
PRICE_CUT_FRAC = 0.02   # a cut of at least 2% counts


def _days_on_market(first_seen_at, link_dead_at, now) -> int | None:
    if not first_seen_at:
        return None
    start = first_seen_at if first_seen_at.tzinfo else first_seen_at.replace(tzinfo=timezone.utc)
    end = link_dead_at or now
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, (end - start).days)


def compute(*, description: str | None = None, prior_asking: float | None = None,
            asking: float | None = None, first_seen_at=None, link_dead_at=None,
            sale_method: str | None = None, asking_basis: str | None = None,
            now: datetime | None = None) -> dict:
    """Return {"flags": [...], "reasons": [...], "risk": [...], "motivated": bool}.

    flags are motivation tags, risk are caution tags, reasons is the plain-English
    "why" for both. Never raises."""
    now = now or datetime.now(timezone.utc)
    text = (description or "").lower()
    flags: list[str] = []
    reasons: list[str] = []
    risk: list[str] = []

    # --- motivation, from the words ---
    for tag, (pat, why) in _MOTIVATION.items():
        if text and re.search(pat, text):
            flags.append(tag)
            reasons.append(why)

    # --- motivation, from the figures ---
    try:
        if prior_asking and asking and float(asking) < float(prior_asking) * (1 - PRICE_CUT_FRAC):
            flags.append("price_cut")
            reasons.append(f"price cut from ${float(prior_asking):,.0f} to ${float(asking):,.0f}")
    except (TypeError, ValueError):
        pass

    dom = _days_on_market(first_seen_at, link_dead_at, now)
    if dom is not None and dom >= DOM_STALE_DAYS:
        flags.append("stale")
        reasons.append(f"on the market ~{dom} days")

    method = f"{sale_method or ''} {asking_basis or ''}".lower()
    if re.search(r"negotiat|by neg|enquir|price by", method):
        if "negotiation" not in flags:
            flags.append("negotiation")
            reasons.append("selling by negotiation (no fixed price)")

    # --- risk, from the words (unless the text says it's already resolved) ---
    if text and not _RISK_RESOLVED.search(text):
        for tag, (pat, why) in _RISK.items():
            if re.search(pat, text):
                risk.append(tag)
                reasons.append(why)

    # de-dup, keep order
    flags = list(dict.fromkeys(flags))
    risk = list(dict.fromkeys(risk))
    return {"flags": flags, "risk": risk, "reasons": reasons,
            "motivated": bool(flags)}


# --- SQL filters, for searching the live batch without a new column ------------
# The compute() above LABELS a row precisely (with the "already re-clad" suppression);
# these express the same signals as SQLAlchemy predicates on the columns we already
# store, so a search can filter and paginate in the database. Deliberately a touch
# broader than compute() (no negation), because for a FILTER surfacing a maybe-leaky
# home to check is the safe error, and the precise label is shown on the row itself.
_SQL_KEYWORDS = {
    "mortgagee": ("mortgagee",),
    "urgent": ("urgent", "must sell", "must be sold", "priced to sell", "vendor says sell"),
    "present_all_offers": ("present all offers", "all offers", "bring all offers", "offers invited"),
    "estate": ("deceased estate", "estate sale", "executor"),
    "leaky": ("reclad", "re-clad", "re clad", "monolithic", "plaster clad", "leaky", "weathertight"),
}


def sql_filter(P, kind: str):
    """A SQLAlchemy predicate selecting listings with the given vendor signal, or
    None for an unknown kind. Works on P.description + structured columns."""
    from sqlalchemy import and_, or_
    kind = (kind or "").strip().lower()

    def kw(name):
        subs = _SQL_KEYWORDS.get(name)
        return or_(*[P.description.ilike(f"%{x}%") for x in subs]) if subs else None

    price_cut = and_(P.prior_asking_price.isnot(None), P.asking_price.isnot(None),
                     P.asking_price < P.prior_asking_price * 0.98)
    stale = and_(P.days_on_market.isnot(None), P.days_on_market >= DOM_STALE_DAYS)
    negotiation = or_(P.sale_method.ilike("%negoti%"), P.asking_basis.ilike("%negoti%"))

    if kind == "price_cut":
        return price_cut
    if kind == "stale":
        return stale
    if kind == "negotiation":
        return negotiation
    if kind in ("leaky", "reclad", "risk"):
        return kw("leaky")
    if kind in _SQL_KEYWORDS:
        return kw(kind)
    if kind == "motivated":
        return or_(kw("mortgagee"), kw("urgent"), kw("present_all_offers"),
                   kw("estate"), price_cut, stale, negotiation)
    return None


def sql_not_leaky(P):
    """Predicate that EXCLUDES likely leaky/reclad homes."""
    from sqlalchemy import not_, or_
    return not_(or_(*[P.description.ilike(f"%{x}%") for x in _SQL_KEYWORDS["leaky"]]))


def flags_string(sig: dict) -> str:
    """A short, ILIKE-searchable, storable tag string, e.g.
    'motivated mortgagee price_cut | risk:reclad'. Empty when nothing fired."""
    parts = list(sig.get("flags", []))
    if sig.get("motivated") and "motivated" not in parts:
        parts = ["motivated"] + parts
    risk = sig.get("risk", [])
    s = " ".join(parts)
    if risk:
        s = (s + " | risk:" + " risk:".join(risk)).strip()
    return s[:120]
