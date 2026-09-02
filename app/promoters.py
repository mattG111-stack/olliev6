"""The referral programme: who brought in which customer, and what they earned.

The rule the whole thing turns on: **a promoter earns when the customer pays**,
not when they sign up and not when they start a trial. A trial is not a payment.
Paying $20 for a trial that never converts is paying out revenue that never
arrived, and at any volume that is the difference between a growth channel and a
leak.

So commission is written from Stripe's `invoice.payment_succeeded` — the event
that means money actually moved. Three things fall out of that for free:

  * A customer who cancels stops earning, with nothing to run and no state to
    reconcile: the invoices simply stop.
  * A failed payment earns nothing, because there was no successful invoice.
  * A customer who pays annually earns for every month the invoice covers,
    because the accrual reads the invoice's own billing period.

Attribution is separate and much simpler: it happens once, at account creation,
and never again.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import (
    DEFAULT_PROMOTER_RATE,
    PROMOTER_RATE,
    Commission,
    Promoter,
    Referral,
    ReferralClick,
    User,
    UserRole,
)

log = logging.getLogger(__name__)

# No 0/O, 1/I/L — a code gets read off a phone screen, typed by someone else,
# and read back over the phone. The characters that look alike are the ones that
# turn a working link into "your link doesn't work".
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8


def normalise_code(raw: str | None) -> str:
    """Codes are case- and punctuation-insensitive coming in, uppercase stored.

    People paste "?ref=matt-b" and type "MATTB"; both have to find the same
    promoter, because the alternative is a promoter losing a signup they earned
    to a capital letter.
    """
    return "".join(c for c in (raw or "").upper() if c.isalnum())[:32]


def generate_code(db: Session, preferred: str | None = None) -> str:
    """A free code — the requested one if it is available, otherwise random."""
    want = normalise_code(preferred)
    if want and len(want) >= 3 and not _taken(db, want):
        return want
    for _ in range(12):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        if not _taken(db, code):
            return code
    # 31^8 is large enough that twelve collisions means something is wrong;
    # lengthening is still better than looping forever or raising.
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN + 4))


def _taken(db: Session, code: str) -> bool:
    return db.query(Promoter).filter(Promoter.code == code).first() is not None


def default_rate(db: Session) -> float:
    from . import settings_store
    raw = settings_store.get(db, PROMOTER_RATE)
    try:
        rate = float(str(raw))
    except (TypeError, ValueError):
        return DEFAULT_PROMOTER_RATE
    return rate if rate >= 0 else DEFAULT_PROMOTER_RATE


def by_code(db: Session, raw: str | None) -> Promoter | None:
    """The promoter behind a referral code, if the code is live.

    A deactivated promoter's link stops attributing NEW signups — that is what
    deactivating is for. It does not touch the customers they already brought
    in; those were earned and keep paying out.
    """
    code = normalise_code(raw)
    if not code:
        return None
    return (db.query(Promoter)
            .filter(Promoter.code == code, Promoter.active.is_(True))
            .first())


def clean_campaign(raw: str | None) -> str | None:
    """A promoter's own label for one ad. Free text, kept short and boring.

    Lowercased and stripped of anything that is not a letter, digit or dash, so
    "Insta Reel — Aug" and "insta-reel-aug" do not become two rows in their own
    breakdown and split the numbers for one ad in half.
    """
    v = "".join(c if (c.isalnum() or c == "-") else "-" for c in (raw or "").strip().lower())
    while "--" in v:
        v = v.replace("--", "-")
    return v.strip("-")[:40] or None


def attribute(db: Session, user: User, raw_code: str | None,
              campaign: str | None = None) -> Referral | None:
    """Credit a brand-new account to a promoter. Called once, at sign-up.

    Every way this can be wrong returns None rather than raising: a referral that
    cannot be recorded must never be the reason someone fails to create an
    account. Losing an attribution costs one commission; losing the signup costs
    the customer.
    """
    promoter = by_code(db, raw_code)
    if promoter is None:
        return None
    if promoter.user_id == user.id:
        # Signing up through your own link is not a customer you recruited.
        log.warning("promoter %s tried to refer themselves", promoter.code)
        return None
    if user.role != UserRole.USER.value:
        # An admin or another promoter is not a customer.
        return None
    if db.query(Referral).filter(Referral.user_id == user.id).first() is not None:
        return None

    ref = Referral(promoter_id=promoter.id, user_id=user.id, code_used=promoter.code,
                   campaign=clean_campaign(campaign))
    db.add(ref)
    try:
        db.commit()
        db.refresh(ref)
    except Exception:
        # Almost certainly the unique constraint: two requests raced for the
        # same account. The row that landed first is the right one.
        db.rollback()
        return db.query(Referral).filter(Referral.user_id == user.id).first()
    log.warning("referral recorded: user=%s promoter=%s code=%s", user.id, promoter.id, promoter.code)
    return ref


def periods_between(start: datetime, end: datetime) -> list[str]:
    """Every 'YYYY-MM' a billing period touches, first to last.

    A monthly invoice gives one. An annual one gives twelve, which is the point:
    a customer who paid for a year is a paying customer for a year, and the
    promoter is owed for each of those months, not for one of them.
    """
    if end < start:
        start, end = end, start
    out: list[str] = []
    y, m = start.year, start.month
    # An end exactly on a month boundary belongs to the month before it — a
    # period of 1 Mar to 1 Apr is March, not March and April.
    last_y, last_m = (end.year, end.month)
    if end.day == 1 and end.hour == 0 and end.minute == 0 and (end.year, end.month) != (y, m):
        last_m -= 1
        if last_m == 0:
            last_m, last_y = 12, last_y - 1
    while (y, m) <= (last_y, last_m) and len(out) < 36:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out or [f"{start.year:04d}-{start.month:02d}"]


def record_payment(db: Session, user: User, start: datetime, end: datetime,
                   *, source: str = "invoice") -> list[Commission]:
    """The customer paid. Write the promoter's commission for the months covered.

    Idempotent by construction: (referral_id, period) is unique, so a webhook
    Stripe retries — and Stripe does retry — cannot pay the same month twice.
    Returns only the rows this call actually created.
    """
    ref = db.query(Referral).filter(Referral.user_id == user.id).first()
    if ref is None:
        return []                       # not a referred customer; nothing owed
    promoter = db.get(Promoter, ref.promoter_id)
    if promoter is None:
        return []

    if ref.first_paid_at is None:
        ref.first_paid_at = datetime.now(timezone.utc)

    made: list[Commission] = []
    for period in periods_between(start, end):
        existing = (db.query(Commission)
                    .filter(Commission.referral_id == ref.id, Commission.period == period)
                    .first())
        if existing is not None:
            continue
        row = Commission(promoter_id=promoter.id, referral_id=ref.id, period=period,
                         amount=promoter.rate, source=source)
        db.add(row)
        try:
            db.commit()
            db.refresh(row)
            made.append(row)
        except Exception:
            # Lost a race with a concurrent webhook for the same month. The
            # other one wrote it; that is the correct outcome, not an error.
            db.rollback()
    try:
        db.commit()
    except Exception:
        db.rollback()
    if made:
        log.warning("commission recorded: promoter=%s user=%s periods=%s rate=%.2f",
                    promoter.id, user.id, [c.period for c in made], promoter.rate)
    return made


def record_click(db: Session, raw_code: str, visitor: str,
                 campaign: str | None = None) -> bool:
    """Someone opened a promoter's link. True if this was a new visitor-day.

    Deduped to one row per visitor per promoter per day by a unique constraint,
    so a refresh, a back button, or reading the page over two sittings is one
    interested person rather than four clicks. Silent on every failure: this is
    a vanity metric attached to a public endpoint, and it must never be able to
    slow down or break the page a real visitor is trying to read.
    """
    promoter = by_code(db, raw_code)
    if promoter is None:
        return False
    v = "".join(c for c in (visitor or "") if c.isalnum() or c == "-")[:40]
    if len(v) < 8:
        return False
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = ReferralClick(promoter_id=promoter.id, visitor=v, day=day,
                        campaign=clean_campaign(campaign))
    db.add(row)
    try:
        db.commit()
        return True
    except Exception:
        db.rollback()          # already counted today, which is the point
        return False


def click_counts(db: Session, promoter_id: int) -> dict:
    """Link opens: all time, the last 30 days, and how many distinct people."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    base = db.query(ReferralClick).filter(ReferralClick.promoter_id == promoter_id)
    return {
        "clicks": base.count(),
        "clicks_30d": base.filter(ReferralClick.day >= since).count(),
        "visitors": (db.query(func.count(func.distinct(ReferralClick.visitor)))
                     .filter(ReferralClick.promoter_id == promoter_id).scalar() or 0),
    }


def _pct(part: int, whole: int) -> float | None:
    """None, not zero, when there is nothing to divide by.

    A conversion rate printed as 0% on a link nobody has opened reads as "this
    is going badly" when the truth is "nothing has happened yet". They are
    different things and the dashboard should not confuse them.
    """
    if not whole:
        return None
    return round(100.0 * part / whole, 1)


# ---- what the dashboards read ----------------------------------------------

ACTIVE_PAID = "active"


def stats(db: Session, promoter: Promoter) -> dict:
    """The promoter's own numbers.

    "Paying" means the customer's subscription is `active` — they have a card
    charged and are past any trial. Trialing is counted and shown SEPARATELY,
    because it is the honest answer to "how am I doing": real interest that has
    not yet earned anything.
    """
    rows = (db.query(Referral, User)
            .join(User, User.id == Referral.user_id)
            .filter(Referral.promoter_id == promoter.id)
            .all())

    paying = trialing = signed_up = lapsed = 0
    for ref, u in rows:
        status = (u.subscription_status or "").lower()
        if status == ACTIVE_PAID:
            paying += 1
        elif status == "trialing":
            trialing += 1
        elif ref.first_paid_at is not None:
            lapsed += 1          # paid at some point, not paying now
        else:
            signed_up += 1       # made an account, never paid

    earned = (db.query(func.coalesce(func.sum(Commission.amount), 0.0))
              .filter(Commission.promoter_id == promoter.id).scalar() or 0.0)
    unpaid = (db.query(func.coalesce(func.sum(Commission.amount), 0.0))
              .filter(Commission.promoter_id == promoter.id,
                      Commission.paid_at.is_(None)).scalar() or 0.0)

    clicks = click_counts(db, promoter.id)
    return {
        "code": promoter.code,
        "rate": promoter.rate,
        # The funnel, in the order it happens. Each step is measured, not
        # inferred from the one before it.
        "clicks": clicks["clicks"],
        "clicks_30d": clicks["clicks_30d"],
        "visitors": clicks["visitors"],
        "click_to_signup": _pct(len(rows), clicks["visitors"]),
        "signup_to_paying": _pct(paying, len(rows)),
        "active": promoter.active,
        "paying": paying,
        "trialing": trialing,
        "signed_up": signed_up,
        "lapsed": lapsed,
        "total_referred": len(rows),
        # What this month will be worth if nothing changes. Not a promise: a
        # customer who cancels before their invoice does not earn it.
        "monthly_run_rate": round(paying * promoter.rate, 2),
        "earned_all_time": round(float(earned), 2),
        "awaiting_payout": round(float(unpaid), 2),
    }


def campaign_rows(db: Session, promoter: Promoter) -> list[dict]:
    """One row per ad the promoter has tagged: opens, signups, paying, earned.

    This is the question a promoter actually has after their second week — not
    "how am I doing" but "which of these is worth making more of". Untagged
    traffic is grouped under its own row rather than dropped, because a link
    shared without a tag is still real and pretending otherwise would make the
    totals here disagree with the totals above it.
    """
    names: set[str | None] = set()
    for (c,) in db.query(ReferralClick.campaign).filter(
            ReferralClick.promoter_id == promoter.id).distinct().all():
        names.add(c)
    for (c,) in db.query(Referral.campaign).filter(
            Referral.promoter_id == promoter.id).distinct().all():
        names.add(c)

    out = []
    for name in names:
        clicks = (db.query(func.count(func.distinct(ReferralClick.visitor)))
                  .filter(ReferralClick.promoter_id == promoter.id,
                          ReferralClick.campaign.is_(None) if name is None
                          else ReferralClick.campaign == name).scalar() or 0)
        refs = (db.query(Referral, User).join(User, User.id == Referral.user_id)
                .filter(Referral.promoter_id == promoter.id,
                        Referral.campaign.is_(None) if name is None
                        else Referral.campaign == name).all())
        paying = sum(1 for _, u in refs if (u.subscription_status or "").lower() == ACTIVE_PAID)
        # Summed from the ledger for the same reason as the referral rows: what
        # was actually recorded, not what today's rate would make it.
        earned = 0.0
        if refs:
            earned = float(db.query(func.coalesce(func.sum(Commission.amount), 0.0))
                           .filter(Commission.referral_id.in_([r.id for r, _ in refs]))
                           .scalar() or 0.0)
        out.append({
            "campaign": name or "",
            "clicks": int(clicks),
            "signups": len(refs),
            "paying": paying,
            "earned": round(earned, 2),
            "click_to_signup": _pct(len(refs), int(clicks)),
        })
    out.sort(key=lambda r: (-r["paying"], -r["signups"], -r["clicks"]))
    return out


def referral_rows(db: Session, promoter: Promoter) -> list[dict]:
    """The referral list a promoter is allowed to see.

    Deliberately carries NO personal information — no email, no name, no
    address. A promoter needs to know how many customers they have and whether
    each is paying. They do not need a list of the customers' email addresses,
    and handing one to an influencer is a privacy incident waiting for a reason.
    """
    rows = (db.query(Referral, User)
            .join(User, User.id == Referral.user_id)
            .filter(Referral.promoter_id == promoter.id)
            .order_by(Referral.created_at.desc())
            .all())
    out = []
    for ref, u in rows:
        status = (u.subscription_status or "").lower()
        if status == ACTIVE_PAID:
            state = "paying"
        elif status == "trialing":
            state = "trialing"
        elif ref.first_paid_at is not None:
            state = "lapsed"
        else:
            state = "signed_up"
        # Months and money are read SEPARATELY from the ledger, and the money is
        # summed rather than derived from months x rate. They are the same thing
        # only while the rate has never changed: multiply by the current rate
        # after a rate change and this row disagrees with the total at the top of
        # the same screen, which is the one discrepancy a promoter is guaranteed
        # to notice.
        months, earned = (db.query(func.count(Commission.id),
                                   func.coalesce(func.sum(Commission.amount), 0.0))
                          .filter(Commission.referral_id == ref.id).one())
        out.append({
            "id": ref.id,
            "joined": ref.created_at,
            "state": state,
            "months_paid": int(months or 0),
            "earned": round(float(earned or 0.0), 2),
        })
    return out
