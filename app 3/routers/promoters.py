"""The referral programme's API: one dashboard for the promoter, one for the admin.

Two audiences with very different rights. A promoter sees their own numbers and
nothing else — not other promoters, and not who their customers are. An admin
sees everyone, creates promoters, sets rates, and records payouts.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import promo_kit
from .. import promoters as P
from .. import settings_store
from ..assistant import providers
from ..config import settings
from ..db import get_db
from ..models import (
    PROMOTER_RATE,
    AssistantLog,
    Commission,
    PromoAsset,
    Promoter,
    Referral,
    User,
    UserRole,
    UserStatus,
)
from ..security import (
    PasswordHashingUnavailable,
    find_user_by_email,
    hash_password,
    require_admin,
    require_promoter,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/promoter", tags=["promoter"])
admin_router = APIRouter(prefix="/api/admin/promoters", tags=["admin"])


def _link(code: str) -> str:
    base = (settings.app_base_url or "").rstrip("/")
    return f"{base}/sign-up?ref={code}"


# ---- the promoter's own dashboard -------------------------------------------

class ReferralOut(BaseModel):
    """One referred customer, with no way to identify them.

    A promoter is told how many customers they have and whether each is paying.
    They are NOT told who those customers are: no email, no name. Handing an
    influencer a list of the platform's customers is a privacy incident with a
    business reason attached, which does not make it less of one.
    """
    id: int
    joined: datetime | None = None
    state: str
    months_paid: int
    earned: float


class ClickIn(BaseModel):
    """A link open, reported by the visitor's own browser.

    No authentication, because the person who clicked has no account yet — that
    is the whole point of the number. `visitor` is a random id the browser made
    up for itself; nothing here identifies anyone.
    """
    code: str = Field(default="", max_length=32)
    # Which ad this open came from, if the promoter tagged the link.
    campaign: str | None = Field(default=None, max_length=40)
    # No minimum length here on purpose. A malformed beacon should be dropped
    # silently by record_click, not answered with a 422 — this endpoint is
    # called from a page a stranger is reading, and a validation error in their
    # console is worse than an uncounted click.
    visitor: str = Field(default="", max_length=40)


class DashboardOut(BaseModel):
    code: str
    link: str
    rate: float
    active: bool
    # The funnel. Clicks are the only one of these that cannot be verified —
    # see the note on the model — so the dashboard says so rather than
    # presenting all four with equal confidence.
    clicks: int = 0
    clicks_30d: int = 0
    visitors: int = 0
    click_to_signup: float | None = None
    signup_to_paying: float | None = None
    paying: int
    trialing: int
    signed_up: int
    lapsed: int
    total_referred: int
    monthly_run_rate: float
    earned_all_time: float
    awaiting_payout: float
    referrals: list[ReferralOut]


@router.post("/click", status_code=204)
def click(body: ClickIn, db: Session = Depends(get_db)) -> Response:
    """Count a link open. Public, and answers 204 whatever happens.

    Deliberately incapable of failing loudly. It is called from a page a
    stranger is reading, before they have an account, and an analytics counter
    must never be able to make that page slower or noisier than it would have
    been without it.
    """
    try:
        P.record_click(db, body.code, body.visitor, body.campaign)
    except Exception:
        log.exception("could not record a referral click")
    return Response(status_code=204)


def _mine(db: Session, me: User) -> Promoter:
    row = db.query(Promoter).filter(Promoter.user_id == me.id).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="This account is not set up as a promoter yet. An admin has to "
                   "create the referral link.")
    return row


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(me: User = Depends(require_promoter),
              db: Session = Depends(get_db)) -> DashboardOut:
    p = _mine(db, me)
    return DashboardOut(link=_link(p.code), **P.stats(db, p),
                        referrals=[ReferralOut(**r) for r in P.referral_rows(db, p)])


# ---- the ad pack -------------------------------------------------------------
#
# 20 MB a file. Big enough for a logo pack, a set of story-sized images or a
# short clip; small enough that the database does not become a file server by
# accident. Anything bigger is a link, and the admin page says so rather than
# failing with a number.
MAX_ASSET_BYTES = 20 * 1024 * 1024

ASSET_KINDS = ("logo", "image", "video", "doc", "link")


class AssetOut(BaseModel):
    id: int
    title: str
    kind: str
    note: str | None = None
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int = 0
    url: str | None = None
    active: bool = True
    created_at: datetime | None = None
    # True when there are bytes to download from us, as opposed to a link out.
    downloadable: bool = False


def _asset_out(a: PromoAsset) -> AssetOut:
    return AssetOut(id=a.id, title=a.title, kind=a.kind, note=a.note,
                    filename=a.filename, content_type=a.content_type,
                    size_bytes=a.size_bytes or 0, url=a.url, active=a.active,
                    created_at=a.created_at, downloadable=bool(a.size_bytes))


def _assets(db: Session, *, active_only: bool) -> list[PromoAsset]:
    q = db.query(PromoAsset)
    if active_only:
        q = q.filter(PromoAsset.active.is_(True))
    # The columns are listed explicitly so the bytes are NOT loaded: a list of
    # twelve assets would otherwise pull ~100 MB through the app to render a
    # table of names.
    return (q.order_by(PromoAsset.sort_order, PromoAsset.id.desc())
            .options()
            .all())


@router.get("/assets", response_model=list[AssetOut])
def my_assets(me: User = Depends(require_promoter),
              db: Session = Depends(get_db)) -> list[AssetOut]:
    """The ad pack, as the promoter sees it. Hidden items are not listed."""
    _mine(db, me)
    return [_asset_out(a) for a in _assets(db, active_only=True)]


@router.get("/assets/{asset_id}/file")
def download_asset(asset_id: int, me: User = Depends(require_promoter),
                   db: Session = Depends(get_db)) -> Response:
    """The bytes. Behind the promoter gate on purpose — an unreleased campaign
    image on a public URL is out before it launches."""
    a = db.get(PromoAsset, asset_id)
    if a is None or not a.active or not a.data:
        raise HTTPException(status_code=404, detail="No such file")
    return Response(
        content=a.data,
        media_type=a.content_type or "application/octet-stream",
        headers={"Content-Disposition":
                 f'attachment; filename="{(a.filename or a.title)[:120]}"'},
    )


class CampaignOut(BaseModel):
    campaign: str
    clicks: int
    signups: int
    paying: int
    earned: float
    click_to_signup: float | None = None


@router.get("/campaigns", response_model=list[CampaignOut])
def campaigns(me: User = Depends(require_promoter),
              db: Session = Depends(get_db)) -> list[CampaignOut]:
    """How each of their ads is doing. Untagged traffic is its own row."""
    p = _mine(db, me)
    return [CampaignOut(**r) for r in P.campaign_rows(db, p)]


@router.get("/kit", response_model=dict)
def kit(me: User = Depends(require_promoter), db: Session = Depends(get_db)) -> dict:
    """The media pack: what the product is, what may not be claimed, the colours,
    and ready-made copy with their own link already in it."""
    p = _mine(db, me)
    pack = promo_kit.media_pack(_link(p.code))
    pack["ads_remaining"] = max(0, promo_kit.DAILY_ADS - promo_kit.ads_used_today(db, me.id))
    pack["ads_limit"] = promo_kit.DAILY_ADS
    provider, key = settings_store.shared_key(db)
    pack["ai_available"] = bool(provider and key)
    return pack


class AdIn(BaseModel):
    channel: str = Field(default="Instagram caption", max_length=80)
    angle: str = Field(default="", max_length=400)
    campaign: str | None = Field(default=None, max_length=40)


@router.post("/ads", response_model=dict)
def write_ads(body: AdIn, me: User = Depends(require_promoter),
              db: Session = Depends(get_db)) -> dict:
    """Draft three ads with the account's shared AI key.

    Capped per promoter per day, and much more tightly than Ask Ollie: this runs
    on the business's key, and one promoter with an idea and a free evening
    should not be able to spend the whole account's allowance on captions.
    """
    p = _mine(db, me)
    used = promo_kit.ads_used_today(db, me.id)
    if used >= promo_kit.DAILY_ADS:
        raise HTTPException(
            status_code=429,
            detail=f"You have drafted {used} sets of ads today, which is the daily "
                   f"limit. The ready-made copy below has no limit.")

    provider, key = settings_store.shared_key(db)
    if not provider or not key:
        raise HTTPException(
            status_code=503,
            detail="AI drafting is not switched on for this account yet. The "
                   "ready-made copy below works without it.")

    # The link carries the campaign tag, so an ad written for one channel is
    # measurable as that channel without the promoter having to build the URL.
    link = _link(p.code)
    tag = P.clean_campaign(body.campaign)
    if tag:
        link = f"{link}&c={tag}"

    try:
        drafts = promo_kit.generate_ads(db, p, link, channel=body.channel,
                                        angle=body.angle, provider=provider, api_key=key)
    except providers.ProviderError as exc:
        db.add(AssistantLog(user_id=me.id, question=f"[ad-copy] {body.channel}",
                            answer=str(exc)[:2000], ok=False, region="ad-copy"))
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Logged with region='ad-copy' so it is metered and reviewable separately
    # from Ask Ollie's questions rather than polluting that log.
    db.add(AssistantLog(user_id=me.id, question=f"[ad-copy] {body.channel} / {body.angle[:120]}",
                        answer=json.dumps(drafts)[:8000], ok=True, region="ad-copy"))
    try:
        db.commit()
    except Exception:
        db.rollback()

    return {"drafts": drafts, "link": link,
            "remaining": max(0, promo_kit.DAILY_ADS - (used + 1))}


# ---- admin ------------------------------------------------------------------

class PromoterOut(BaseModel):
    id: int
    user_id: int
    clicks: int = 0
    clicks_30d: int = 0
    visitors: int = 0
    click_to_signup: float | None = None
    signup_to_paying: float | None = None
    email: str
    full_name: str | None = None
    code: str
    link: str
    rate: float
    active: bool
    payout_email: str | None = None
    created_at: datetime | None = None
    paying: int
    trialing: int
    signed_up: int
    lapsed: int
    total_referred: int
    monthly_run_rate: float
    earned_all_time: float
    awaiting_payout: float


def _out(db: Session, p: Promoter) -> PromoterOut:
    u = db.get(User, p.user_id)
    return PromoterOut(
        id=p.id, user_id=p.user_id,
        email=(u.email if u else "(deleted account)"),
        full_name=(u.full_name if u else None),
        link=_link(p.code), payout_email=p.payout_email, created_at=p.created_at,
        **P.stats(db, p),
    )


@router.get("/rate", response_model=dict)
def current_rate(_: User = Depends(require_promoter), db: Session = Depends(get_db)) -> dict:
    return {"rate": P.default_rate(db)}


@admin_router.get("", response_model=list[PromoterOut])
def list_promoters(_: User = Depends(require_admin),
                   db: Session = Depends(get_db)) -> list[PromoterOut]:
    rows = db.query(Promoter).order_by(Promoter.created_at.desc()).all()
    return [_out(db, p) for p in rows]


class CreateIn(BaseModel):
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=160)
    # Optional: an existing account can be turned into a promoter instead.
    password: str | None = Field(default=None, min_length=8, max_length=128)
    code: str | None = Field(default=None, max_length=32)
    rate: float | None = Field(default=None, ge=0, le=10_000)
    payout_email: EmailStr | None = None


@admin_router.post("", response_model=PromoterOut, status_code=201)
def create_promoter(body: CreateIn, me: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> PromoterOut:
    """Make someone a promoter, creating their login if they do not have one.

    Turning an EXISTING customer into a promoter is refused. Their account is
    already counted in the paying customers a promoter is supposed to bring in,
    and the two roles pulling on the same row is the kind of ambiguity that only
    shows up when the money is wrong.
    """
    existing = find_user_by_email(db, str(body.email))
    if existing is not None:
        if existing.role == UserRole.ADMIN.value:
            raise HTTPException(status_code=409,
                                detail="That is an admin account. Use a separate address for the promoter login.")
        if existing.role == UserRole.USER.value:
            raise HTTPException(
                status_code=409,
                detail="That address already belongs to a customer account. Promoters need "
                       "their own login — use a different address.")
        if db.query(Promoter).filter(Promoter.user_id == existing.id).first():
            raise HTTPException(status_code=409, detail="That promoter already exists.")
        user = existing
    else:
        if not body.password:
            raise HTTPException(status_code=422,
                                detail="Set a password for the new promoter login (at least 8 characters).")
        try:
            pw = hash_password(body.password)
        except PasswordHashingUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        user = User(email=str(body.email).lower(), password_hash=pw,
                    full_name=body.full_name, role=UserRole.PROMOTER.value,
                    status=UserStatus.APPROVED.value, signup_source="admin")
        db.add(user)
        try:
            db.commit(); db.refresh(user)
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=503,
                                detail=f"Could not create the login: {type(exc).__name__}: {exc}") from exc

    p = Promoter(user_id=user.id, code=P.generate_code(db, body.code),
                 display_name=body.full_name or user.full_name,
                 rate=body.rate if body.rate is not None else P.default_rate(db),
                 payout_email=str(body.payout_email) if body.payout_email else str(body.email))
    db.add(p)
    try:
        db.commit(); db.refresh(p)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503,
                            detail=f"Could not create the promoter: {type(exc).__name__}: {exc}") from exc
    log.warning("promoter created by %s: %s code=%s rate=%.2f", me.email, user.email, p.code, p.rate)
    return _out(db, p)


class UpdateIn(BaseModel):
    rate: float | None = Field(default=None, ge=0, le=10_000)
    active: bool | None = None
    payout_email: EmailStr | None = None
    code: str | None = Field(default=None, max_length=32)


@admin_router.patch("/{promoter_id}", response_model=PromoterOut)
def update_promoter(promoter_id: int, body: UpdateIn, me: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> PromoterOut:
    p = db.get(Promoter, promoter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="No such promoter")
    if body.rate is not None:
        p.rate = body.rate
    if body.active is not None:
        p.active = body.active
    if body.payout_email is not None:
        p.payout_email = str(body.payout_email)
    if body.code:
        want = P.normalise_code(body.code)
        if len(want) < 3:
            raise HTTPException(status_code=422, detail="A code needs at least 3 letters or digits.")
        clash = db.query(Promoter).filter(Promoter.code == want, Promoter.id != p.id).first()
        if clash is not None:
            raise HTTPException(status_code=409, detail="Another promoter already uses that code.")
        # Changing a code does not un-attribute anyone: referrals record which
        # code was used, and the link between customer and promoter is an id.
        p.code = want
    try:
        db.commit(); db.refresh(p)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Could not save: {type(exc).__name__}: {exc}") from exc
    log.warning("promoter %s updated by %s (rate=%.2f active=%s code=%s)",
                p.id, me.email, p.rate, p.active, p.code)
    return _out(db, p)


class RateIn(BaseModel):
    rate: float = Field(ge=0, le=10_000)


@admin_router.put("/rate", response_model=dict)
def set_default_rate(body: RateIn, me: User = Depends(require_admin),
                     db: Session = Depends(get_db)) -> dict:
    """The rate NEW promoters are signed at. Existing promoters keep theirs —
    changing what someone already agreed to, retroactively and silently, is not
    a thing this should be able to do by accident."""
    try:
        settings_store.put(db, PROMOTER_RATE, str(body.rate), by=me.id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not save the rate: {exc}") from exc
    log.warning("default promoter rate set to %.2f by %s", body.rate, me.email)
    return {"rate": body.rate}


class CommissionOut(BaseModel):
    id: int
    promoter_id: int
    promoter_email: str | None = None
    code: str | None = None
    period: str
    amount: float
    source: str
    created_at: datetime | None = None
    paid_at: datetime | None = None
    payout_ref: str | None = None


@admin_router.get("/commissions", response_model=list[CommissionOut])
def commissions(unpaid_only: bool = False, period: str | None = None,
                _: User = Depends(require_admin),
                db: Session = Depends(get_db)) -> list[CommissionOut]:
    q = (db.query(Commission, Promoter, User)
         .join(Promoter, Promoter.id == Commission.promoter_id)
         .outerjoin(User, User.id == Promoter.user_id))
    if unpaid_only:
        q = q.filter(Commission.paid_at.is_(None))
    if period:
        q = q.filter(Commission.period == period)
    rows = q.order_by(Commission.period.desc(), Commission.id.desc()).limit(2000).all()
    return [CommissionOut(id=c.id, promoter_id=c.promoter_id,
                          promoter_email=(u.email if u else None), code=p.code,
                          period=c.period, amount=c.amount, source=c.source,
                          created_at=c.created_at, paid_at=c.paid_at, payout_ref=c.payout_ref)
            for c, p, u in rows]


class PayoutIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    promoter_id: int | None = None
    payout_ref: str | None = Field(default=None, max_length=120)


@admin_router.post("/payouts", response_model=dict)
def mark_paid(body: PayoutIn, me: User = Depends(require_admin),
              db: Session = Depends(get_db)) -> dict:
    """Mark a month's commissions as paid out. Already-paid rows are left alone,
    so running it twice does not rewrite the first payout's reference."""
    q = db.query(Commission).filter(Commission.period == body.period,
                                    Commission.paid_at.is_(None))
    if body.promoter_id is not None:
        q = q.filter(Commission.promoter_id == body.promoter_id)
    rows = q.all()
    now = datetime.now(timezone.utc)
    total = 0.0
    for r in rows:
        r.paid_at = now
        r.payout_ref = body.payout_ref
        total += r.amount
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Could not record the payout: {exc}") from exc
    log.warning("payout recorded by %s: period=%s rows=%d total=%.2f",
                me.email, body.period, len(rows), total)
    return {"period": body.period, "marked": len(rows), "total": round(total, 2)}


class ManualIn(BaseModel):
    """A commission recorded by hand.

    Exists because Stripe is not the only way money arrives, and because a
    webhook that was missed while something was misconfigured still leaves a
    promoter genuinely owed. Recorded with source='manual' so the ledger always
    says whether a row is evidence or a decision.
    """
    user_email: EmailStr
    period: str = Field(pattern=r"^\d{4}-\d{2}$")


@admin_router.post("/commissions/manual", response_model=CommissionOut, status_code=201)
def add_manual(body: ManualIn, me: User = Depends(require_admin),
               db: Session = Depends(get_db)) -> CommissionOut:
    user = find_user_by_email(db, str(body.user_email))
    if user is None:
        raise HTTPException(status_code=404, detail="No account with that address.")
    ref = db.query(Referral).filter(Referral.user_id == user.id).first()
    if ref is None:
        raise HTTPException(status_code=404,
                            detail="That customer was not referred by anyone, so there is no commission to record.")
    if db.query(Commission).filter(Commission.referral_id == ref.id,
                                   Commission.period == body.period).first():
        raise HTTPException(status_code=409,
                            detail=f"{body.period} is already recorded for that customer.")
    p = db.get(Promoter, ref.promoter_id)
    row = Commission(promoter_id=ref.promoter_id, referral_id=ref.id, period=body.period,
                     amount=(p.rate if p else 0.0), source="manual")
    db.add(row)
    try:
        db.commit(); db.refresh(row)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Could not record it: {exc}") from exc
    log.warning("manual commission by %s: user=%s period=%s", me.email, user.email, body.period)
    u = db.get(User, p.user_id) if p else None
    return CommissionOut(id=row.id, promoter_id=row.promoter_id,
                         promoter_email=(u.email if u else None), code=(p.code if p else None),
                         period=row.period, amount=row.amount, source=row.source,
                         created_at=row.created_at, paid_at=None, payout_ref=None)


@admin_router.get("/export.csv")
def export_csv(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> StreamingResponse:
    """Everything owed and paid, as a spreadsheet — this is what a payout run
    actually gets done from."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["promoter", "email", "code", "period", "amount", "source",
                "recorded", "paid_at", "payout_ref"])
    rows = (db.query(Commission, Promoter, User)
            .join(Promoter, Promoter.id == Commission.promoter_id)
            .outerjoin(User, User.id == Promoter.user_id)
            .order_by(Commission.period.desc(), Commission.promoter_id).all())
    for c, p, u in rows:
        w.writerow([p.display_name or (u.full_name if u else "") or "", u.email if u else "",
                    p.code, c.period, f"{c.amount:.2f}", c.source,
                    c.created_at.isoformat() if c.created_at else "",
                    c.paid_at.isoformat() if c.paid_at else "", c.payout_ref or ""])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="commissions.csv"'})


# ---- the ad pack, admin side -------------------------------------------------

@admin_router.get("/assets", response_model=list[AssetOut])
def list_assets(_: User = Depends(require_admin),
                db: Session = Depends(get_db)) -> list[AssetOut]:
    """Everything in the pack, including the hidden items."""
    return [_asset_out(a) for a in _assets(db, active_only=False)]


@admin_router.post("/assets", response_model=AssetOut, status_code=201)
async def upload_asset(
    me: User = Depends(require_admin),
    db: Session = Depends(get_db),
    file: UploadFile | None = File(None),
    title: str = Form(""),
    kind: str = Form("image"),
    note: str = Form(""),
    url: str = Form(""),
    sort_order: int = Form(0),
) -> AssetOut:
    """Add a file or a link to the ad pack.

    A file is read into memory and stored as a row, so it survives the deploys
    that wipe the container's disk. That is only reasonable up to a point, and
    the point is MAX_ASSET_BYTES — past it the answer is a link, and the error
    says so rather than just refusing.
    """
    k = (kind or "image").strip().lower()
    if k not in ASSET_KINDS:
        raise HTTPException(status_code=422,
                            detail=f"Kind must be one of: {', '.join(ASSET_KINDS)}")

    link = (url or "").strip()
    if link and not link.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="A link has to start with http:// or https://")

    data: bytes | None = None
    filename = content_type = None
    if file is not None and (file.filename or "").strip():
        data = await file.read()
        if len(data) > MAX_ASSET_BYTES:
            mb = MAX_ASSET_BYTES // (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=f"That file is {len(data) / 1024 / 1024:.1f} MB and the limit is "
                       f"{mb} MB. Put a big video on YouTube or Drive and add it here "
                       f"as a link instead — promoters get the same thing and the "
                       f"database stays a database.")
        if not data:
            raise HTTPException(status_code=422, detail="That file is empty.")
        filename = (file.filename or "file")[:255]
        content_type = (file.content_type or "application/octet-stream")[:120]

    if data is None and not link:
        raise HTTPException(status_code=422,
                            detail="Choose a file to upload, or paste a link.")

    row = PromoAsset(
        title=(title.strip() or filename or link)[:160],
        kind=k if data is not None else (k if k != "image" or not link else "link"),
        note=(note.strip() or None),
        filename=filename, content_type=content_type,
        size_bytes=len(data) if data else 0, data=data,
        url=link or None, sort_order=sort_order, uploaded_by=me.id,
    )
    db.add(row)
    try:
        db.commit(); db.refresh(row)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503,
                            detail=f"Could not save it: {type(exc).__name__}: {exc}") from exc
    log.warning("promo asset added by %s: %s (%s, %d bytes)",
                me.email, row.title, row.kind, row.size_bytes)
    return _asset_out(row)


class AssetPatch(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    note: str | None = None
    kind: str | None = None
    active: bool | None = None
    sort_order: int | None = None


@admin_router.patch("/assets/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: int, body: AssetPatch, me: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> AssetOut:
    a = db.get(PromoAsset, asset_id)
    if a is None:
        raise HTTPException(status_code=404, detail="No such item")
    if body.title is not None:
        a.title = body.title.strip()[:160] or a.title
    if body.note is not None:
        a.note = body.note.strip() or None
    if body.kind is not None:
        k = body.kind.strip().lower()
        if k not in ASSET_KINDS:
            raise HTTPException(status_code=422, detail=f"Kind must be one of: {', '.join(ASSET_KINDS)}")
        a.kind = k
    if body.active is not None:
        a.active = body.active
    if body.sort_order is not None:
        a.sort_order = body.sort_order
    try:
        db.commit(); db.refresh(a)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Could not save: {exc}") from exc
    return _asset_out(a)


@admin_router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(asset_id: int, me: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> Response:
    a = db.get(PromoAsset, asset_id)
    if a is None:
        raise HTTPException(status_code=404, detail="No such item")
    title = a.title
    db.delete(a)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Could not delete: {exc}") from exc
    log.warning("promo asset deleted by %s: %s", me.email, title)
    return Response(status_code=204)


@admin_router.get("/assets/{asset_id}/file")
def admin_download_asset(asset_id: int, _: User = Depends(require_admin),
                         db: Session = Depends(get_db)) -> Response:
    """Same bytes, but an admin can fetch a hidden item too — otherwise there is
    no way to check what you are about to un-hide."""
    a = db.get(PromoAsset, asset_id)
    if a is None or not a.data:
        raise HTTPException(status_code=404, detail="No such file")
    return Response(content=a.data,
                    media_type=a.content_type or "application/octet-stream",
                    headers={"Content-Disposition":
                             f'attachment; filename="{(a.filename or a.title)[:120]}"'})


@admin_router.get("/summary", response_model=dict)
def summary(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    owed = (db.query(func.coalesce(func.sum(Commission.amount), 0.0))
            .filter(Commission.paid_at.is_(None)).scalar() or 0.0)
    paid = (db.query(func.coalesce(func.sum(Commission.amount), 0.0))
            .filter(Commission.paid_at.isnot(None)).scalar() or 0.0)
    referred_paying = (db.query(func.count(Referral.id))
                       .join(User, User.id == Referral.user_id)
                       .filter(func.lower(User.subscription_status) == "active").scalar() or 0)
    stored = (db.query(func.coalesce(func.sum(PromoAsset.size_bytes), 0)).scalar() or 0)
    return {
        "assets": db.query(func.count(PromoAsset.id)).scalar() or 0,
        # Surfaced because the ad pack lives in the database. Nobody watches a
        # number they cannot see, and "why is the database 4 GB" is a question
        # worth being able to answer in one place.
        "assets_bytes": int(stored),
        "promoters": db.query(func.count(Promoter.id)).scalar() or 0,
        "active_promoters": db.query(func.count(Promoter.id)).filter(Promoter.active.is_(True)).scalar() or 0,
        "referred_total": db.query(func.count(Referral.id)).scalar() or 0,
        "referred_paying": int(referred_paying),
        "owed": round(float(owed), 2),
        "paid_out": round(float(paid), 2),
        "default_rate": P.default_rate(db),
    }
