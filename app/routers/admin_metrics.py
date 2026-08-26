"""Admin operations dashboard — data pipeline + business metrics.

One endpoint the admin dashboard reads: user/login activity, buyer's-agent
enquiries, the state of the weekly data loads, and Stripe billing (revenue +
paying customers). Everything is real except billing, which lights up once a
Stripe key is set (see app.billing).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..billing import billing_metrics, paying_users
from ..db import get_db
from ..models import (AgentContact, AssistantLog, ImportBatch, PageView, User,
                      UserStatus)
from ..security import find_user_by_email, require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


class PageUsage(BaseModel):
    """One feature, and how it is actually used."""
    path: str
    views: int
    users: int              # distinct people, so one heavy user is not a trend
    # MEDIAN, not mean: one tab left open for twenty minutes would otherwise
    # make a page look absorbing when nobody reads it.
    median_seconds: float | None = None


class TopUser(BaseModel):
    """One of the heaviest users of the platform, over the chosen window."""
    email: str
    minutes: float          # total time on page
    views: int
    # Distinct days they showed up. Separates a habit from a binge: 90 minutes
    # across 20 days is a user who relies on this, 90 minutes in one sitting is
    # someone who had a look. Total time alone cannot tell those apart.
    days_active: int = 0
    # Only counts pages that reported a duration. A visitor who closes the tab
    # on the last page of every session gives up that page's time, so this is a
    # floor on real usage rather than an exact figure.
    last_seen: str | None = None


class Metrics(BaseModel):
    # people
    users_total: int
    users_active: int          # approved
    users_new_30d: int
    logins_7d: int             # users seen in last 7 days
    logins_30d: int
    total_logins: int
    # Distinct people who signed in today, not sign-in events: last_login_at
    # holds only the most recent one, so a second sign-in overwrites the first.
    # Counting rows would report the same number and imply a precision the
    # column cannot support.
    users_signed_in_today: int = 0
    # Feature usage. Empty until a build that reports page views has been
    # running for a while — an empty table here means no data yet, not no usage.
    page_views_today: int = 0
    active_users_today: int = 0
    top_pages_7d: list["PageUsage"] = []
    top_users_30d: list["TopUser"] = []
    # The window the two activity tables were built over, echoed back so the
    # screen can state it rather than implying a fixed period it no longer uses.
    activity_days: int = 7
    # sign-ups (self-serve)
    signups_total: int
    signups_7d: int
    signups_30d: int
    # onboarding funnel (self-serve users)
    onboarding_email_verified: int
    onboarding_phone_verified: int
    onboarding_trialing: int
    onboarding_paying: int
    # engagement
    agent_contacts_total: int
    agent_contacts_30d: int
    # billing (Stripe)
    billing_connected: bool
    paying_customers: int
    mrr: float
    income_this_month: float
    currency: str
    billing_error: str | None = None
    # data pipeline
    sold_rows: int             # accumulated comp database size
    sold_last_loaded: str | None
    forsale_rows: int          # current live listings
    forsale_last_loaded: str | None


def _batch(db: Session, batch_type: str):
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == batch_type, ImportBatch.is_active.is_(True))
            .order_by(ImportBatch.id.desc()).first())


@router.get("/metrics", response_model=Metrics)
def metrics(days: int = Query(7, ge=1, le=365),
            me: User = Depends(require_admin),
            db: Session = Depends(get_db)) -> Metrics:
    now = datetime.now(timezone.utc)
    d30 = now - timedelta(days=30)
    d7 = now - timedelta(days=7)
    # The two activity tables run over a window the operator picks; every other
    # figure keeps its fixed period so the headline numbers stay comparable
    # between visits.
    since = now - timedelta(days=days)

    users_total = db.query(func.count(User.id)).scalar() or 0
    users_active = db.query(func.count(User.id)).filter(User.status == UserStatus.APPROVED.value).scalar() or 0
    users_new_30d = db.query(func.count(User.id)).filter(User.created_at >= d30).scalar() or 0
    logins_7d = db.query(func.count(User.id)).filter(User.last_login_at.isnot(None), User.last_login_at >= d7).scalar() or 0
    logins_30d = db.query(func.count(User.id)).filter(User.last_login_at.isnot(None), User.last_login_at >= d30).scalar() or 0
    total_logins = db.query(func.coalesce(func.sum(User.login_count), 0)).scalar() or 0

    contacts_total = db.query(func.count(AgentContact.id)).scalar() or 0
    contacts_30d = db.query(func.count(AgentContact.id)).filter(AgentContact.created_at >= d30).scalar() or 0

    # Self-serve sign-ups (distinct from admin-created accounts).
    self_signups = db.query(User).filter(User.signup_source == "self")
    signups_total = self_signups.count()
    signups_7d = self_signups.filter(User.created_at >= d7).count()
    signups_30d = self_signups.filter(User.created_at >= d30).count()
    ob_email = self_signups.filter(User.email_verified_at.isnot(None)).count()
    ob_phone = self_signups.filter(User.phone_verified_at.isnot(None)).count()
    ob_trialing = db.query(func.count(User.id)).filter(User.subscription_status == "trialing").scalar() or 0
    ob_paying = db.query(func.count(User.id)).filter(User.subscription_status == "active").scalar() or 0

    b = billing_metrics()

    sold = _batch(db, "sold")
    fs = _batch(db, "for_sale")
    # Sold is the accumulated comp DB — count every sold row across batches.
    sold_rows = db.execute(text("SELECT COUNT(*) FROM properties_sold")).scalar() or 0
    fs_rows = fs.rows_inserted if fs else 0

    # ---- who is using what -------------------------------------------------
    # Midnight UTC, matching every other window on this dashboard. Worth knowing
    # it is not midnight in Auckland: "today" here ends at noon local.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    users_signed_in_today = (db.query(func.count(User.id))
                             .filter(User.last_login_at >= midnight).scalar() or 0)
    page_views_today = (db.query(func.count(PageView.id))
                        .filter(PageView.created_at >= midnight).scalar() or 0)
    active_users_today = (db.query(func.count(func.distinct(PageView.user_id)))
                          .filter(PageView.created_at >= midnight).scalar() or 0)

    # Medians in Python rather than SQL percentiles, so this runs on SQLite as
    # well as Postgres — the pattern the market pulse had to be rewritten into
    # after its Postgres-only query could never be exercised outside production.
    top_pages: list[PageUsage] = []
    rows = (db.query(PageView.path, PageView.user_id, PageView.seconds)
            .filter(PageView.created_at >= since).all())
    grouped: dict[str, dict] = {}
    for path, uid, secs in rows:
        g = grouped.setdefault(path, {"views": 0, "users": set(), "secs": []})
        g["views"] += 1
        if uid is not None:
            g["users"].add(uid)
        if secs is not None:
            g["secs"].append(float(secs))
    for path, g in grouped.items():
        s = sorted(g["secs"])
        top_pages.append(PageUsage(
            path=path, views=g["views"], users=len(g["users"]),
            median_seconds=(round(s[len(s) // 2], 1) if s else None)))
    top_pages.sort(key=lambda p: -p.views)

    # Heaviest users by time on the platform. Joined to users so a deleted
    # account cannot appear as a nameless row of usage.
    # Aggregated in Python: distinct-days-active needs the date part of a
    # timestamp, and every portable way to express that in SQL is a dialect
    # special case. The row count here is one per page view in the window, which
    # is small enough not to warrant one.
    top_users: list[TopUser] = []
    per_user: dict[int, dict] = {}
    for uid, secs, at in (db.query(PageView.user_id, PageView.seconds, PageView.created_at)
                          .filter(PageView.created_at >= since,
                                  PageView.user_id.isnot(None)).all()):
        u = per_user.setdefault(uid, {"secs": 0.0, "views": 0, "days": set(), "last": None})
        u["secs"] += float(secs or 0)
        u["views"] += 1
        if at:
            u["days"].add(at.date())
            if u["last"] is None or at > u["last"]:
                u["last"] = at
    if per_user:
        emails = dict(db.query(User.id, User.email)
                      .filter(User.id.in_(list(per_user))).all())
        for uid, u in per_user.items():
            if uid not in emails:
                continue          # deleted account: no nameless rows of usage
            top_users.append(TopUser(
                email=emails[uid],
                minutes=round(u["secs"] / 60, 1),
                views=u["views"],
                days_active=len(u["days"]),
                last_seen=u["last"].isoformat() if u["last"] else None))
        top_users.sort(key=lambda t: -t.minutes)
        top_users = top_users[:10]

    return Metrics(
        activity_days=days,
        top_users_30d=top_users,
        users_signed_in_today=users_signed_in_today,
        page_views_today=page_views_today,
        active_users_today=active_users_today,
        top_pages_7d=top_pages[:25],
        users_total=users_total, users_active=users_active, users_new_30d=users_new_30d,
        logins_7d=logins_7d, logins_30d=logins_30d, total_logins=int(total_logins),
        signups_total=signups_total, signups_7d=signups_7d, signups_30d=signups_30d,
        onboarding_email_verified=ob_email, onboarding_phone_verified=ob_phone,
        onboarding_trialing=ob_trialing, onboarding_paying=ob_paying,
        agent_contacts_total=contacts_total, agent_contacts_30d=contacts_30d,
        billing_connected=b.connected, paying_customers=b.active_subscribers,
        mrr=round(b.mrr, 2), income_this_month=round(b.income_this_month, 2),
        currency=b.currency, billing_error=b.error,
        sold_rows=int(sold_rows),
        sold_last_loaded=sold.created_at.isoformat() if sold and sold.created_at else None,
        forsale_rows=int(fs_rows),
        forsale_last_loaded=fs.created_at.isoformat() if fs and fs.created_at else None,
    )


class PayingUserRow(BaseModel):
    email: str | None
    name: str | None
    amount_monthly: float
    currency: str
    status: str
    since: str | None
    customer_id: str
    app_user_id: int | None = None      # matched to one of our users, if found


class PayingUsers(BaseModel):
    connected: bool
    customers: list[PayingUserRow]


class Interest(BaseModel):
    """What one customer is looking for, in their own actions."""
    user_id: int | None
    email: str
    name: str | None
    last_seen: str | None
    minutes: float = 0.0            # time on the platform in the window
    questions: int = 0
    # What they SAVED — the clearest statement of intent in the product, because
    # they typed it themselves into a wish list.
    suburbs: list[str] = []         # from wish lists and from what they ask about
    price_low: float | None = None
    price_high: float | None = None
    min_beds: int | None = None
    wants: list[str] = []           # "underpriced", "subdividable"
    top_pages: list[str] = []       # where the time goes
    last_questions: list[str] = []  # the three most recent, verbatim


class Interests(BaseModel):
    days: int
    rows: list[Interest] = []


# Suburb names are matched against the ones actually in the data rather than a
# hard-coded list, so this cannot drift out of step with what has been loaded.
def _iso(v) -> str | None:
    """A timestamp as an ISO string, whichever type the driver handed back.

    Raw SQL returns created_at as a datetime on Postgres and as a STRING on
    SQLite. Calling .isoformat() on it therefore works in production and raises
    everywhere else — the same shape as the percentile SQL that made suburb
    trends untestable outside production. Normalising here means the rest of
    this function never has to know which database it is talking to, and the
    values stay comparable because ISO strings sort chronologically.
    """
    if v is None:
        return None
    iso = getattr(v, "isoformat", None)
    return iso() if callable(iso) else str(v)


def _known_suburbs(db: Session) -> list[str]:
    rows = db.execute(text(
        "SELECT DISTINCT suburb FROM properties_for_sale WHERE suburb IS NOT NULL"
    )).fetchall()
    return [r[0] for r in rows if r[0]]


@router.get("/interests", response_model=Interests)
def user_interests(days: int = Query(30, ge=1, le=365),
                   _: User = Depends(require_admin),
                   db: Session = Depends(get_db)) -> Interests:
    """Know each customer by what they are looking at.

    Built from what people DO in the product, in this order of directness:

      * their wish lists — a suburb, a price band and a bed count they typed in
        themselves, which is as close to a stated brief as this product gets
      * the questions they ask Ollie — the suburbs in them, and the last few
        verbatim, because "is Glenfield still soft?" says something a filter
        cannot
      * where their time goes — which parts of the product they actually use

    NOT built from which listings they opened. page_views records the route and
    never the id, on purpose (see the model): the question that table answers is
    which features get used, and keeping ids would turn it into a record of who
    looked at whose house. That decision is left standing here.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    suburb_names = _known_suburbs(db)
    lowered = [(s, s.lower()) for s in suburb_names]

    users = (db.query(User)
             .filter(User.role != "promoter")
             .order_by(User.id.asc()).all())
    by_id = {u.id: u for u in users}

    # time + favourite pages
    mins: dict[int, float] = {}
    pages: dict[int, dict[str, float]] = {}
    last: dict[int, str] = {}
    for uid, path, secs, at in db.execute(text(
        "SELECT user_id, path, seconds, created_at FROM page_views "
        "WHERE created_at >= :since AND user_id IS NOT NULL"
    ), {"since": since}).fetchall():
        if uid not in by_id:
            continue
        s_ = float(secs or 0)
        mins[uid] = mins.get(uid, 0.0) + s_ / 60.0
        pages.setdefault(uid, {}).setdefault(path, 0.0)
        pages[uid][path] += s_
        at_iso = _iso(at)
        if at_iso and (uid not in last or at_iso > last[uid]):
            last[uid] = at_iso

    # what they saved
    saved: dict[int, dict] = {}
    for row in db.execute(text(
        "SELECT user_id, suburb, district, min_price, max_price, min_beds, "
        "       underpriced_only, subdividable_only FROM wish_lists"
    )).fetchall():
        uid = row[0]
        if uid not in by_id:
            continue
        d = saved.setdefault(uid, {"suburbs": set(), "lo": None, "hi": None,
                                   "beds": None, "wants": set()})
        for place in (row[1], row[2]):
            if place:
                d["suburbs"].add(place)
        if row[3] is not None:
            d["lo"] = row[3] if d["lo"] is None else min(d["lo"], float(row[3]))
        if row[4] is not None:
            d["hi"] = row[4] if d["hi"] is None else max(d["hi"], float(row[4]))
        if row[5] is not None:
            d["beds"] = row[5] if d["beds"] is None else min(d["beds"], int(row[5]))
        if row[6]:
            d["wants"].add("underpriced")
        if row[7]:
            d["wants"].add("subdividable")

    # what they ask about
    asked: dict[int, list[str]] = {}
    asked_suburbs: dict[int, set[str]] = {}
    for uid, question, at in db.execute(text(
        "SELECT user_id, question, created_at FROM assistant_logs "
        "WHERE user_id IS NOT NULL ORDER BY id DESC"
    )).fetchall():
        if uid not in by_id:
            continue
        asked.setdefault(uid, [])
        if len(asked[uid]) < 3:
            asked[uid].append((question or "")[:180])
        ql = (question or "").lower()
        for name, low in lowered:
            if low and low in ql:
                asked_suburbs.setdefault(uid, set()).add(name)
        at_iso = _iso(at)
        if at_iso and (uid not in last or at_iso > last[uid]):
            last[uid] = at_iso

    q_counts: dict[int, int] = {
        uid: int(n) for uid, n in
        db.query(AssistantLog.user_id, func.count(AssistantLog.id))
          .filter(AssistantLog.user_id.isnot(None))
          .group_by(AssistantLog.user_id).all()
    }

    rows: list[Interest] = []
    for u in users:
        sv = saved.get(u.id, {})
        suburbs = sorted(set(sv.get("suburbs", set())) | asked_suburbs.get(u.id, set()))
        top = sorted(pages.get(u.id, {}).items(), key=lambda kv: -kv[1])[:3]
        seen = last.get(u.id)
        row = Interest(
            user_id=u.id, email=u.email, name=u.full_name,
            last_seen=seen,
            minutes=round(mins.get(u.id, 0.0), 1),
            questions=q_counts.get(u.id, 0),
            suburbs=suburbs[:8],
            price_low=sv.get("lo"), price_high=sv.get("hi"),
            min_beds=sv.get("beds"),
            wants=sorted(sv.get("wants", set())),
            top_pages=[p for p, _sec in top],
            last_questions=asked.get(u.id, []),
        )
        # Someone who has done nothing tells you nothing; leave them off.
        if (row.minutes or row.questions or row.suburbs or row.wants
                or row.price_low or row.price_high):
            rows.append(row)

    rows.sort(key=lambda r: (-(r.minutes or 0), -(r.questions or 0)))
    return Interests(days=days, rows=rows)


@router.get("/paying-users", response_model=PayingUsers)
def paying_users_list(me: User = Depends(require_admin), db: Session = Depends(get_db)) -> PayingUsers:
    """All active Stripe subscribers, matched to our user records by Stripe
    customer id or email. Empty (connected=False) until Stripe is configured."""
    b = billing_metrics()
    rows = []
    for c in paying_users():
        u = None
        if c.customer_id:
            u = db.query(User).filter(User.stripe_customer_id == c.customer_id).first()
        if u is None and c.email:
            u = find_user_by_email(db, c.email)
        rows.append(PayingUserRow(
            email=c.email, name=c.name, amount_monthly=c.amount_monthly, currency=c.currency,
            status=c.status, since=c.since, customer_id=c.customer_id,
            app_user_id=u.id if u else None,
        ))
    return PayingUsers(connected=b.connected, customers=rows)
