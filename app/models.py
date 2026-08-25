from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    LargeBinary,
    UniqueConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class UserRole(str, Enum):
    USER = "user"
    ADMIN = "admin"
    # An influencer or promoter. Not a customer: they have no subscription and
    # see no listings, only their own referral dashboard. Kept as a role rather
    # than a flag on a normal account so a promoter cannot accidentally be
    # counted as one of the paying customers they are supposed to be recruiting.
    PROMOTER = "promoter"


class UserStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEACTIVATED = "deactivated"


class BatchType(str, Enum):
    FOR_SALE = "for_sale"
    SOLD = "sold"
    RENT = "rent"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    company: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.USER.value, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=UserStatus.PENDING.value, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Links this user to their Stripe customer so the admin dashboard can read
    # their subscription/revenue. Set when a subscription is created.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # --- self-serve onboarding -------------------------------------------------
    # A self-serve signup walks: verify email → verify phone → add card → trial.
    # Product access is granted once they're trialing/paying (see security.
    # has_product_access); admins and admin-approved users bypass this.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cached from Stripe via webhook: 'trialing' | 'active' | 'past_due' |
    # 'canceled' | 'incomplete' | None (never subscribed).
    subscription_status: Mapped[str | None] = mapped_column(String(24), index=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signup_source: Mapped[str | None] = mapped_column(String(16))   # 'self' | 'admin'

    # --- assistant credentials ---------------------------------------------
    # The user's own LLM key, encrypted at rest (see assistant/keys.py). There
    # is no read path back to the plaintext through the API — only "is it set"
    # and the last four characters.
    llm_provider: Mapped[str | None] = mapped_column(String(16))
    llm_api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    llm_key_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Admin-provisioned key: the assistant works but the user can't see, change,
    # or remove the key, and the bring-your-own-key panel is hidden from them.
    llm_key_managed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WishList(Base):
    """A user's saved search / watch list. Matched against the active for-sale
    batch; new matches (vs the batch the user last viewed) drive the in-app
    notification badge."""
    __tablename__ = "wish_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # criteria (all optional — an empty wish list matches everything)
    district: Mapped[str | None] = mapped_column(String(64))
    suburb: Mapped[str | None] = mapped_column(String(120))
    property_category: Mapped[str | None] = mapped_column(String(24))   # house/townhouse/...
    min_price: Mapped[float | None] = mapped_column(Float)
    max_price: Mapped[float | None] = mapped_column(Float)
    min_beds: Mapped[int | None] = mapped_column(Integer)
    underpriced_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    subdividable_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # "developments under a value": subdividable sites whose buy price is <= this
    max_dev_buy_price: Mapped[float | None] = mapped_column(Float)
    # the for-sale batch the user last viewed these matches against
    last_seen_batch_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PageView(Base):
    """One visit to one page, with how long it was open.

    Answers which features people actually use, which no other table can: the
    assistant log shows who asked Ollie something, agent contacts show who
    enquired, and everything else in between was invisible. A feature nobody
    opens and a feature everyone opens and immediately leaves look identical
    without this.

    Written once per visit, when the page is LEFT, so the row carries its own
    duration rather than needing two events matched up afterwards. That also
    means the current page is missing until the visitor moves — deliberate:
    counting an open tab as engagement would make an abandoned session look like
    the most-used feature in the product.

    A brand-new table, so create_all builds it on boot — no migration needed.
    """
    __tablename__ = "page_views"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    # The route, never the full URL: a property page is "/property" rather than
    # "/property/8213". The question is which features get used, and keeping ids
    # would turn a usage table into a record of who looked at whose house.
    path: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AssistantLog(Base):
    """Every question asked of Ollie's assistant, with its answer — the memory the
    assistant learns from. Two uses: (1) the user's own recent Q&A is fed back as
    context so the assistant remembers what they've been exploring across sessions;
    (2) the full log is the review/training corpus for improving answers over time.
    A brand-new table, so create_all builds it on boot — no migration needed."""
    __tablename__ = "assistant_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # False = errored / no answer
    tools_used: Mapped[str | None] = mapped_column(Text)   # JSON list of tool names
    region: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_type: Mapped[str] = mapped_column(String(16), nullable=False)
    region: Mapped[str] = mapped_column(String(64), default="Auckland", nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Two-stage publish lifecycle: a batch is ingested as "staged" (loaded but not
    # live), reviewed, then "published" (is_active flips true, old one archived).
    #   staged → published → archived
    status: Mapped[str] = mapped_column(String(16), default="staged", nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    note: Mapped[str | None] = mapped_column(Text)


class IngestJob(Base):
    """Tracks a single async ingest task. Created the moment a file lands;
    background task updates status as it progresses."""
    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_type: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    file_path: Mapped[str | None] = mapped_column(String(1024))  # temp path on disk
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)  # pending|running|completed|failed
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    # `stage` is a SHORT human-readable label ONLY — "load", "enrich", "price",
    # "publish", "done", "error". It is varchar(64); never serialise a result dict
    # into it (that raised StringDataRightTruncation when the publish result was
    # written here). Structured results belong in result_json below.
    stage: Mapped[str | None] = mapped_column(String(64))  # human-readable current stage
    rows_total: Mapped[int | None] = mapped_column(Integer)
    rows_inserted: Mapped[int | None] = mapped_column(Integer)
    rows_rejected: Mapped[int | None] = mapped_column(Integer)
    # Durable enrich progress: CoreLogic returns nothing for many addresses, which
    # is a normal outcome, not a failure — filled vs missed must be distinguishable
    # and must survive a page refresh / browser disconnect (progress lives in the
    # DB, not the tab). rows_filled = cells filled; rows_missed = lookups that
    # returned nothing.
    rows_filled: Mapped[int | None] = mapped_column(Integer, default=0)
    rows_missed: Mapped[int | None] = mapped_column(Integer, default=0)
    # Structured result of a stage (e.g. the publish result dict), as JSON text.
    # Keeps payloads out of the short `stage` label.
    result_json: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    # JSON-encoded list of anomaly warnings found by the post-ingest audit
    # (e.g. extreme market values, suspicious correction lookups). Distinct
    # from error_message: warnings don't fail the ingest, they flag rows
    # for human review on the admin uploads page.
    audit_warnings: Mapped[str | None] = mapped_column(Text)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Shared columns the scraper produces for every property listing.
class _PropertyMixin:
    address: Mapped[str | None] = mapped_column(String(512))
    name: Mapped[str | None] = mapped_column(String(512))

    suburb: Mapped[str | None] = mapped_column(String(128), index=True)
    district: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(64), default="Auckland", index=True)
    postcode: Mapped[str | None] = mapped_column(String(16))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    property_type: Mapped[str | None] = mapped_column(String(128))
    type_of_title: Mapped[str | None] = mapped_column(Text)
    zoning: Mapped[str | None] = mapped_column(String(128), index=True)
    land_slope_contour: Mapped[str | None] = mapped_column(String(128))

    beds: Mapped[int | None] = mapped_column(Integer)
    baths: Mapped[int | None] = mapped_column(Integer)
    cars: Mapped[int | None] = mapped_column(Integer)
    floor_area_m2: Mapped[float | None] = mapped_column(Float)
    land_area_m2: Mapped[float | None] = mapped_column(Float)

    cv_numeric: Mapped[float | None] = mapped_column(Float)
    land_value_numeric: Mapped[float | None] = mapped_column(Float)
    improvement_value_numeric: Mapped[float | None] = mapped_column(Float)

    url: Mapped[str | None] = mapped_column(String(1024))
    slug_id: Mapped[str | None] = mapped_column(String(128), index=True)
    image_url: Mapped[str | None] = mapped_column(String(1024))
    image_count: Mapped[int | None] = mapped_column(Integer)
    # Every listing photo, newline-separated in scrape order. The scrape carries
    # up to 20; storing only the first meant the card and detail page could never
    # show a gallery.
    image_urls: Mapped[str | None] = mapped_column(Text)

    listing_date: Mapped[str | None] = mapped_column(String(32))
    days_on_market: Mapped[float | None] = mapped_column(Float)

    # === Raw scraper fields we preserve for future use ===
    key_facts: Mapped[str | None] = mapped_column(Text)
    key_time_on_market: Mapped[str | None] = mapped_column(String(64))
    estate_description: Mapped[str | None] = mapped_column(Text)
    council_valuation_summary: Mapped[str | None] = mapped_column(Text)
    property_trend: Mapped[str | None] = mapped_column(Text)
    sale_status: Mapped[str | None] = mapped_column(String(32))
    last_updated: Mapped[str | None] = mapped_column(String(32))

    # === Third-party reference valuation (legacy column names retained for DB compat) ===
    third_party_valuation: Mapped[float | None] = mapped_column("hg_valuation", Float)
    third_party_valuation_high: Mapped[float | None] = mapped_column("hg_valuation_high", Float)
    third_party_valuation_low: Mapped[float | None] = mapped_column("hg_valuation_low", Float)
    valuation_last_date: Mapped[str | None] = mapped_column(String(32))

    # === OneRoof's own estimate =============================================
    # It used to be written into third_party_valuation above, which is the
    # HOUGARDEN figure that arrives with the weekly feed. Every enrich run
    # therefore overwrote Hougarden's number with OneRoof's and destroyed the
    # original — so the column stopped meaning one portal, the property page
    # showed a figure labelled as one source and holding another, and the
    # accuracy panel measured "us versus Hougarden" against a mixture of the
    # two depending on which properties happened to have been enriched.
    #
    # Every other portal has its own columns. This one did not, for no better
    # reason than that nobody added them.
    oneroof_valuation: Mapped[float | None] = mapped_column(Float)
    oneroof_valuation_low: Mapped[float | None] = mapped_column(Float)
    oneroof_valuation_high: Mapped[float | None] = mapped_column(Float)
    oneroof_url: Mapped[str | None] = mapped_column(String(300))

    # === Trade Me's own figure, from their sales export ===
    # Shown to the reader as "Trade Me says", and NEVER used as an input to any
    # valuation, signal or comparison. It is not an independent estimate: for a
    # property that has sold, it is that property's own sale price carried
    # forward — measured across 54,692 Auckland sales, their figure lands a
    # median 1.2% from the actual price, and the gap converges smoothly on zero
    # as the sale gets more recent. Useful to display, worthless as a check.
    tm_valuation: Mapped[float | None] = mapped_column(Float)
    tm_valuation_low: Mapped[float | None] = mapped_column(Float)
    tm_valuation_high: Mapped[float | None] = mapped_column(Float)
    tm_valuation_date: Mapped[str | None] = mapped_column(String(32))

    # === CV change tracking ===
    valuation_rateable_change_pct: Mapped[float | None] = mapped_column(Float)
    valuation_land_change_pct: Mapped[float | None] = mapped_column(Float)
    valuation_improvement_change_pct: Mapped[float | None] = mapped_column(Float)

    # === Last sale of this property ===
    valuation_last_sold_value: Mapped[float | None] = mapped_column(Float)
    valuation_last_sold_date: Mapped[str | None] = mapped_column(String(32))
    sold_listing_date: Mapped[str | None] = mapped_column(String(32))
    sold_listing_price_label: Mapped[str | None] = mapped_column(String(128))

    # === Trend JSONs — for charts ===
    valuation_trend_yearly_json: Mapped[str | None] = mapped_column(Text)
    valuation_trend_monthly_json: Mapped[str | None] = mapped_column(Text)
    sale_history_json: Mapped[str | None] = mapped_column(Text)
    cv_history_json: Mapped[str | None] = mapped_column(Text)
    schools_json: Mapped[str | None] = mapped_column(Text)

    # === Agent contact ===
    agent1_name: Mapped[str | None] = mapped_column(String(255))
    agent1_phone: Mapped[str | None] = mapped_column(String(64))
    agent1_email: Mapped[str | None] = mapped_column(String(255))
    agent1_job_title: Mapped[str | None] = mapped_column(String(128))
    agent1_company_name: Mapped[str | None] = mapped_column(String(255))
    agent2_name: Mapped[str | None] = mapped_column(String(255))
    agent2_phone: Mapped[str | None] = mapped_column(String(64))
    agent2_email: Mapped[str | None] = mapped_column(String(255))
    agent2_job_title: Mapped[str | None] = mapped_column(String(128))
    agent2_company_name: Mapped[str | None] = mapped_column(String(255))
    company_name: Mapped[str | None] = mapped_column(String(255))

    # === Other potentially-present scraper fields ===
    building_age: Mapped[str | None] = mapped_column(String(32))
    parking_covered: Mapped[int | None] = mapped_column(Integer)
    parking_other: Mapped[int | None] = mapped_column(Integer)
    has_swimming_pool: Mapped[bool | None] = mapped_column(Boolean)
    is_new_construction: Mapped[bool | None] = mapped_column(Boolean)
    is_coastal_waterfront: Mapped[bool | None] = mapped_column(Boolean)
    storey_count: Mapped[int | None] = mapped_column(Integer)
    other_features: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    listing_title: Mapped[str | None] = mapped_column(String(512))
    listing_published_date: Mapped[str | None] = mapped_column(String(32))


class PropertyForSale(_PropertyMixin, Base):
    __tablename__ = "properties_for_sale"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # raw scrape
    asking_price: Mapped[float | None] = mapped_column(Float)

    # v3.8 model output (market_value = pred_v38 rounded to nearest $1k)
    market_value: Mapped[float | None] = mapped_column(Float)
    predicted_list: Mapped[float | None] = mapped_column(Float)
    predicted_days: Mapped[float | None] = mapped_column(Float)
    comps_used: Mapped[int | None] = mapped_column(Integer)  # legacy; now stores n_subtype
    confidence: Mapped[str | None] = mapped_column(String(16))
    pred_vs_cv: Mapped[float | None] = mapped_column(Float)
    pred_vs_listing: Mapped[float | None] = mapped_column(Float)
    # v3.8 diagnostic fields — explain how the estimate was computed
    pred_v35: Mapped[float | None] = mapped_column(Float)
    pred_v38: Mapped[float | None] = mapped_column(Float)
    z_weight: Mapped[float | None] = mapped_column(Float)
    beta_tier: Mapped[str | None] = mapped_column(String(16))
    cv_anchor: Mapped[float | None] = mapped_column(Float)
    cv_ratio_tier: Mapped[str | None] = mapped_column(String(16))
    correction_used: Mapped[str | None] = mapped_column(String(16))

    # === v4 production AVM (June 2026) ===
    # Two-tier: asking × 0.95 when usable, else v3.5 hedonic fallback.
    listing_type: Mapped[str | None] = mapped_column(String(16))      # fixed | auction | tender | negotiation | unknown
    pricing_path: Mapped[str | None] = mapped_column(String(16))      # asking | v35 | insufficient
    range_low: Mapped[float | None] = mapped_column(Float)            # honest price range — low end
    range_high: Mapped[float | None] = mapped_column(Float)           # honest price range — high end
    subdivision_premium: Mapped[float | None] = mapped_column(Float)  # separate line item, NOT folded into market_value
    # Independent fair value (CV-bounded hedonic) + margin vs asking for deal-finding.
    fair_value: Mapped[float | None] = mapped_column(Float)
    margin: Mapped[float | None] = mapped_column(Float)               # (fair_value/asking - 1); positive = potential deal
    is_premium: Mapped[bool | None] = mapped_column(Boolean, default=False)  # ultra-prime: priced off listing, no model valuation/deal

    # === Acquisition layer (buy price) — cascade comps, June 2026 spec ===
    buy_price: Mapped[float | None] = mapped_column(Float)            # 0.95 × MIN(asking, area_value)
    area_value: Mapped[float | None] = mapped_column(Float)           # v4 × avg(comp_sale / comp_v4)
    comp_tier: Mapped[int | None] = mapped_column(Integer)            # 1–6 (tightest tier with 2+ comps); NULL = v4 fallback
    comps_matched: Mapped[int | None] = mapped_column(Integer)        # number of comps used

    # === Subdivision (§6 section-rate model) ===
    sections: Mapped[int | None] = mapped_column(Integer)             # number of sections the site splits into
    dwellings: Mapped[int | None] = mapped_column(Integer)            # sections × max dwellings per lot
    section_rate: Mapped[float | None] = mapped_column(Float)         # $/m² (from sold land values)
    gross_sales: Mapped[float | None] = mapped_column(Float)          # sections × section value
    subdivision_profit: Mapped[float | None] = mapped_column(Float)   # gross sales − buy − costs

    # subdivision
    min_lot_m2: Mapped[float | None] = mapped_column(Float)
    max_addl_lots: Mapped[float | None] = mapped_column(Float)
    section_price_per_m2: Mapped[float | None] = mapped_column(Float)
    section_value_method: Mapped[str | None] = mapped_column(String(32))
    services_cost: Mapped[float | None] = mapped_column(Float)
    total_subdivided_value: Mapped[float | None] = mapped_column(Float)
    uplift_vs_asking: Mapped[float | None] = mapped_column(Float)

    # cashflow
    est_weekly_rent: Mapped[float | None] = mapped_column(Float)
    est_gross_yield: Mapped[float | None] = mapped_column(Float)
    annual_gross_rent: Mapped[float | None] = mapped_column(Float)
    annual_net_rent: Mapped[float | None] = mapped_column(Float)
    annual_mortgage: Mapped[float | None] = mapped_column(Float)
    annual_cashflow: Mapped[float | None] = mapped_column(Float)
    cash_on_cash: Mapped[float | None] = mapped_column(Float)
    breakeven_deposit_pct: Mapped[float | None] = mapped_column(Float, index=True)
    # What this will transact at (asking x 0.95 when listed, else CV x area ratio).
    # Distinct from fair_value, which is deliberately computed WITHOUT the asking
    # price so the margin means something.
    expected_sale: Mapped[float | None] = mapped_column(Float, index=True)
    expected_sale_path: Mapped[str | None] = mapped_column(String(32))
    expected_sale_band: Mapped[float | None] = mapped_column(Float)

    # scoring
    opportunity_score: Mapped[float | None] = mapped_column(Float, index=True)
    opportunity_score_pct: Mapped[float | None] = mapped_column(Float, index=True)  # 0-100 normalized
    best_strategy: Mapped[str | None] = mapped_column(String(32))
    best_net_gain: Mapped[float | None] = mapped_column(Float)
    is_underpriced: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_cashflow_positive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_subdividable: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Weekly pre-publish verification against the listing's own page (app.verify):
    # the land area the source shows, and a verdict — "ok" | "mismatch" |
    # "unverified" (couldn't read the page). A mismatch means the stored land area
    # is wrong (e.g. 139 Long Drive stored 5,665 m² vs 416 m² on the listing) and
    # the area-dependent signals (subdivision) are held back for it.
    land_area_listing_m2: Mapped[float | None] = mapped_column(Float)
    land_area_flag: Mapped[str | None] = mapped_column(String(16), index=True)

    # On-demand external estimate (homes.co.nz), cached per property so it's
    # fetched once, not every page view. See app.external_estimates.
    homes_valuation: Mapped[float | None] = mapped_column(Float)
    homes_valuation_low: Mapped[float | None] = mapped_column(Float)
    homes_valuation_high: Mapped[float | None] = mapped_column(Float)
    homes_cv: Mapped[float | None] = mapped_column(Float)
    homes_url: Mapped[str | None] = mapped_column(String(300))
    homes_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # realestate.co.nz — slot reserved; populated once a data source is wired in.
    realestate_valuation: Mapped[float | None] = mapped_column(Float)
    realestate_valuation_low: Mapped[float | None] = mapped_column(Float)
    realestate_valuation_high: Mapped[float | None] = mapped_column(Float)
    realestate_url: Mapped[str | None] = mapped_column(String(300))

    # propertyvalue.co.nz (CoreLogic) — on-demand, cached. Flat fields feed the
    # Compare tile; pv_data holds the full record (attrs/zoning/CV/sale) that the
    # verification cross-check compares against our own data. See app.propertyvalue.
    pv_estimate_low: Mapped[float | None] = mapped_column(Float)
    pv_estimate_high: Mapped[float | None] = mapped_column(Float)
    pv_estimate_mid: Mapped[float | None] = mapped_column(Float)
    pv_cv: Mapped[float | None] = mapped_column(Float)
    pv_url: Mapped[str | None] = mapped_column(String(400))
    # CoreLogic's last recorded sale (their data, kept separate from our own
    # valuation_last_sold_* which the pipeline owns). Grabbed for every deal.
    pv_last_sale_price: Mapped[float | None] = mapped_column(Float)
    pv_last_sale_date: Mapped[str | None] = mapped_column(String(32))
    pv_data: Mapped[str | None] = mapped_column(Text)          # JSON of pv_lookup()
    pv_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the portals were last asked about this property (app/portals). Stamped
    # even when nobody recognised the address, so a run resumes rather than
    # re-asking five sources about a place none of them know.
    portals_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Two-stage publish: a row flagged during the pre-publish review is HELD back —
    # its batch can go live while this row stays hidden until an admin fixes it and
    # publishes it individually. Held rows are excluded from all live views.
    is_held: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    hold_reason: Mapped[str | None] = mapped_column(String(300))

    # CV reconciliation (scripts/reconcile_cv): when our CV and homes' CV disagree,
    # the market (asking vs CV × the suburb sale ratio) decides which is credible.
    # "ok" = our CV is backed; "suspect" = the market sides with homes' CV, so our
    # CV — and any deal signal it drove — is untrustworthy for this listing.
    cv_flag: Mapped[str | None] = mapped_column(String(16), index=True)

    __table_args__ = (
        Index("ix_fs_active_suburb", "import_batch_id", "suburb"),
        Index("ix_fs_active_score", "import_batch_id", "opportunity_score_pct"),
    )


class PropertySold(_PropertyMixin, Base):
    __tablename__ = "properties_sold"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sale_price: Mapped[float | None] = mapped_column(Float, index=True)
    sold_date: Mapped[str | None] = mapped_column(String(32), index=True)
    sale_method: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_sold_comp", "import_batch_id", "suburb", "beds", "baths"),
        Index("ix_sold_zoning", "import_batch_id", "zoning"),
    )


class PropertyRent(_PropertyMixin, Base):
    __tablename__ = "properties_rent"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    weekly_rent: Mapped[float | None] = mapped_column(Float, index=True)
    listing_date_rent: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_rent_comp", "import_batch_id", "suburb", "beds"),
    )


class AgentContact(Base):
    """A user reaching out to Ollie's buyer's agent about a property — logged so
    the admin dashboard can count enquiries. One row per contact action."""
    __tablename__ = "agent_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    property_id: Mapped[int | None] = mapped_column(index=True)
    address: Mapped[str | None] = mapped_column(String(255))
    suburb: Mapped[str | None] = mapped_column(String(120))
    channel: Mapped[str | None] = mapped_column(String(16))   # "email" | "phone"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class VerificationCode(Base):
    """A short-lived code sent to a user's email or phone during onboarding.
    One row per issued code; the newest unconsumed, unexpired row for a channel
    is the valid one. Codes are single-use (consumed_at) with an attempt cap."""
    __tablename__ = "verification_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)   # "email" | "phone"
    code: Mapped[str] = mapped_column(String(12), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ParcelCache(Base):
    """A legal parcel boundary from LINZ, kept so we ask them once per property.

    Boundaries do not move, so this is cached indefinitely rather than on a TTL —
    the only reason to refetch is a subdivision, which arrives as a new title and
    therefore a new lookup at a point the old ring no longer contains.

    `ring` is JSON: a list of [lat, lng] pairs, already closed. `status` records a
    miss ("none") as well as a hit, so a property outside LINZ's coverage isn't
    re-queried on every page view.
    """
    __tablename__ = "parcel_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The lookup point, rounded to ~1 m, which is what makes this a cache key.
    lat_key: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    lng_key: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)   # "linz" | "none"
    ring: Mapped[str | None] = mapped_column(Text)
    area_m2: Mapped[float | None] = mapped_column(Float)
    appellation: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_parcel_point", "lat_key", "lng_key", unique=True),
    )


class BuildingOverride(Base):
    """A hand-placed building for the Sun & shade panel — one row per building.

    The shade that decides whether a property is cold in winter is almost never
    its own: it is the two-storey place on the north boundary. Nothing in the
    listing data describes a neighbour's building, so they are placed by hand
    here, and every one of them casts.

    The subject dwelling is stored the same way (is_subject), because the
    footprint derived from floor area is a rectangle on the pin and rarely sits
    squarely on the roof in the photo — and the shadow is cast FROM that
    rectangle, so a footprint in the wrong place puts the shade in the wrong
    place.

    Stored as centre offset + size + rotation rather than a ring: it is what the
    editor produces, it survives a change of imagery provider, and it stays
    meaningful if the listing's coordinates are corrected later.
    """
    __tablename__ = "building_overrides"

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(index=True, nullable=False)
    # Ordering within a property, so edits round-trip in a stable order.
    idx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_subject: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Metres east / north of the listing's own lat-lng.
    east_m: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    north_m: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    width_m: Mapped[float] = mapped_column(Float, nullable=False)
    depth_m: Mapped[float] = mapped_column(Float, nullable=False)
    rot_deg: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    height_m: Mapped[float] = mapped_column(Float, default=3.5, nullable=False)
    label: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_building_property_idx", "property_id", "idx"),
    )


class AppSetting(Base):
    """One row per application-wide setting, set by an admin and read by everyone.

    The assistant's API key lives here rather than on a user row. It used to be
    per-user: every person had to go and get their own Claude or OpenAI key
    before Ollie would answer anything, which is a wall in front of the feature
    for everyone who is not technical. One key set once by the admin serves the
    whole account, with a daily cap per user so a single enthusiastic afternoon
    cannot run up the bill.

    Values are stored as text; a caller that wants a number parses it. That keeps
    the table honest about what it is — a small key/value store for things an
    operator sets, not a schema that needs a migration each time one is added.
    """
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        server_default=func.now())
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


# Setting keys, named in one place so a typo cannot silently read a different row.
# The Apify token, set in the admin panel rather than only as an environment
# variable. Same shape and same encryption as the assistant key: a token typed
# into a browser must not be readable by anyone who gets a look at the database.
# The environment variable still works and still wins — a value in Railway is
# what a deploy is reproducible from, and a value typed into a form is what an
# operator can change on a Sunday without one.
APIFY_TOKEN = "apify.token_encrypted"

LLM_PROVIDER = "llm.provider"
LLM_API_KEY = "llm.api_key_encrypted"
LLM_DAILY_LIMIT = "llm.daily_limit"

# How many questions one user may ask a day on the shared key, unless an admin
# changes it. A user who has added their own key is not counted or capped — they
# are paying for it.
DEFAULT_DAILY_LIMIT = 20

# Aerial imagery. These were NEXT_PUBLIC_ build-time variables on the frontend,
# which meant changing or rotating a maps key required a rebuild and a redeploy
# of the whole site — and a key set without also setting the provider name did
# nothing at all, which is indistinguishable from a key that does not work.
# Held here instead, an admin sets one in the panel and it takes effect on the
# next page load.
MAPS_PROVIDER = "maps.provider"
MAPS_GOOGLE_KEY = "maps.google_key_encrypted"
MAPS_LINZ_KEY = "maps.linz_key_encrypted"


class TrainedModel(Base):
    """A valuation fitted on our own sales, kept with the numbers that judged it.

    Every model ever fitted is a row here, not just the live one. Three reasons,
    and all three have bitten somebody before:

    A model you cannot roll back to is a model you cannot ship confidently. If a
    retrain quietly makes 10,900 prices worse, the fix is to reactivate the
    previous row, not to reconstruct last month's coefficients from memory.

    A model without its own scores is an unfalsifiable claim. `forward_error`,
    `engine_error` and `raw_cv_error` are measured on sales the fit never saw,
    at the moment it was fitted, and stored beside it — so "is this better than
    what we had" is answered by reading two rows rather than by re-running
    anything.

    And a model that lives only in the memory of the process that fitted it
    reverts to the spreadsheet on the next redeploy, silently.

    `payload` is the whole thing as JSON - coefficients, the imputation medians,
    the suburb effects. Around 17 KB for the current feature set, so a Text
    column is the right home and there is no file to lose.
    """
    __tablename__ = "trained_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), default="valuation",
                                      nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[str | None] = mapped_column(Text)

    n_train: Mapped[int | None] = mapped_column(Integer)
    n_test: Mapped[int | None] = mapped_column(Integer)
    # Median % error on held-out sales, measured forward: fitted on the past,
    # tested on the future, which is the only way it is ever actually used.
    forward_error: Mapped[float | None] = mapped_column(Float)
    engine_error: Mapped[float | None] = mapped_column(Float)
    raw_cv_error: Mapped[float | None] = mapped_column(Float)

    # Did it earn the right to be used, and the sentence explaining the verdict.
    # A model that failed the gate is still kept: knowing that three retrains in
    # a row failed is how you find out the incoming data has a problem.
    shipped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verdict: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False,
                                            nullable=False, index=True)

    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    trained_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


# Whether the trained valuation is actually used to price listings, or only
# measured. Off until someone turns it on, deliberately: a switch that defaults
# to on changes every price on the site the first time a model is fitted, and
# nobody asked for that.
ML_VALUATION_ENABLED = "ml.valuation_enabled"


class Promoter(Base):
    """An influencer or promoter who earns per paying customer they bring in.

    One row per promoter account, holding the referral code and the rate. The
    rate is stored per promoter, not read from a global at payout time: someone
    signed at $20 keeps $20 if the standard rate later changes, which is what
    they agreed to and the only version that survives an argument about it.
    """
    __tablename__ = "promoters"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True,
                                         nullable=False)
    # What goes in the link. Case-insensitive on the way in; stored uppercase.
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160))
    # Dollars per referred customer per month they pay. Per promoter, so a
    # different deal for one person does not need a schema change.
    rate: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    # A deactivated promoter's link stops attributing new signups. It does NOT
    # stop commission on customers they already brought in — those were earned.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    payout_email: Mapped[str | None] = mapped_column(String(255))
    payout_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Referral(Base):
    """One customer, attributed to one promoter, once and forever.

    user_id is UNIQUE. That is the whole anti-double-counting rule in one
    constraint: an account has exactly one referrer, set when the account is
    created and never afterwards. Two promoters cannot both claim the same
    customer, and a customer who later clicks someone else's link does not
    change hands.

    Attribution happens at account creation only. Someone who already has an
    account is not a new customer no matter whose link they arrive through.
    """
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(primary_key=True)
    promoter_id: Mapped[int] = mapped_column(ForeignKey("promoters.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True,
                                         nullable=False)
    code_used: Mapped[str] = mapped_column(String(32), nullable=False)
    # The ad that produced this customer, carried through from the link they
    # clicked. This is what turns "you have 4 customers" into "the reel works
    # and the story does not".
    campaign: Mapped[str | None] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When this customer's first invoice was actually paid. Null while they are
    # signed up, or trialing, and have never paid — which earns nothing.
    first_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReferralClick(Base):
    """Someone opened a promoter's link.

    One row per visitor per promoter per DAY, not per page load. A person who
    opens the link, reads for ten minutes, comes back after dinner and refreshes
    twice is one interested person, and counting that as five clicks flatters the
    number in a way that makes the whole panel useless for deciding anything.

    Recorded from the browser, so it counts people who ran the page's JavaScript.
    That is an undercount — ad blockers, privacy modes, and link previews will
    not appear — and it is deliberately the safer direction to be wrong in: this
    number cannot be verified the way a paid invoice can, so it should never be
    the flattering one.

    Nothing identifying is stored. No IP address, no user agent, no location —
    just a random id the visitor's own browser made up, which is enough to tell
    one person from two and nothing else.
    """
    __tablename__ = "referral_clicks"
    __table_args__ = (
        UniqueConstraint("promoter_id", "visitor", "day", name="uq_click_promoter_visitor_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    promoter_id: Mapped[int] = mapped_column(ForeignKey("promoters.id"), index=True, nullable=False)
    visitor: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    day: Mapped[str] = mapped_column(String(10), index=True, nullable=False)   # YYYY-MM-DD
    # Which ad this open came from, if the promoter tagged the link. Free text
    # they choose ("insta-reel-aug"), because only they know what their ads are.
    campaign: Mapped[str | None] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PromoAsset(Base):
    """One item in the ad pack: a logo, an image, a video, a PDF, or a link.

    The bytes live in the DATABASE, not on disk. Railway gives a container a
    filesystem that is wiped on every deploy, so a file written next to the app
    is a file that disappears the next time anything ships — and it would
    disappear silently, with the promoter finding out by clicking a download
    that 404s. A row in Postgres survives deploys, restarts and rollbacks, and
    needs no second service to provision.

    That trade has a limit, which is why `url` exists. A 300 MB video does not
    belong in a database row; it belongs on YouTube or Drive with a link here.
    Uploads are capped and the admin page says what to do instead.
    """
    __tablename__ = "promo_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    # What it is for, in the promoter's terms: 'logo' | 'image' | 'video' |
    # 'doc' | 'link'. Drives how it is shown, not how it is stored.
    kind: Mapped[str] = mapped_column(String(16), default="image", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    filename: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Set instead of `data` for things too big to hold — a video, a Drive folder.
    url: Mapped[str | None] = mapped_column(String(500))

    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Hidden rather than deleted, so something pulled for a legal reason can be
    # put back without re-uploading it.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Commission(Base):
    """One month's earning for one referred customer.

    Written when the customer's invoice is PAID, not when they sign up and not
    when they start a trial. A trial is not a payment, and paying a promoter for
    a trial that never converts is paying out revenue that never arrived.

    (promoter_id, referral_id, period) is unique, so a retried webhook, a
    proration, or two invoices in one month cannot pay the same commission
    twice. period is 'YYYY-MM'.
    """
    __tablename__ = "commissions"
    __table_args__ = (
        UniqueConstraint("referral_id", "period", name="uq_commission_referral_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    promoter_id: Mapped[int] = mapped_column(ForeignKey("promoters.id"), index=True, nullable=False)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id"), index=True, nullable=False)
    period: Mapped[str] = mapped_column(String(7), index=True, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    # What caused it: 'invoice' (Stripe said the customer paid) or 'manual' (an
    # admin recorded it). Worth keeping — one is evidence, the other is a
    # decision, and a payout dispute turns on which.
    source: Mapped[str] = mapped_column(String(16), default="invoice", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payout_ref: Mapped[str | None] = mapped_column(String(120))


# What a promoter earns per referred customer per paid month, unless their own
# row says otherwise. An admin can change it for future promoters; existing
# promoters keep the rate they were signed at.
PROMOTER_RATE = "promoter.rate"
DEFAULT_PROMOTER_RATE = 20.0


class BugReport(Base):
    """One reported fault, with the context that makes it diagnosable.

    Reporting a bug in prose loses the two facts that decide how long it takes to
    fix: which build it happened on, and what the server actually said. A report
    that says "creating users doesn't work" and one that says "v1.3, POST
    /api/admin/users returned 503: cannot hash passwords — bcrypt failed to
    import" are the same sentence to the person writing them and a day apart for
    the person fixing them.

    So the versions and the recent failed requests are captured by the form
    rather than typed, and the whole log exports as CSV.
    """
    __tablename__ = "bug_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), index=True)
    reported_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reported_by_email: Mapped[str | None] = mapped_column(String(320))

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    # Where it happened, as the browser saw it.
    page: Mapped[str | None] = mapped_column(String(500))
    severity: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False, index=True)
    # Free text for what was found or done about it.
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Where the report came from: "manual" (someone filed it), "server" (an
    # unhandled error on the API), "browser" (a crash in the page). Anything
    # other than manual arrived on its own.
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False, index=True)
    # The same fault happening repeatedly is one entry with a count, not fifty
    # rows. A log that floods is a log nobody opens.
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A fingerprint of what went wrong, so repeats can be recognised.
    fingerprint: Mapped[str | None] = mapped_column(String(200), index=True)

    # Captured, not typed.
    app_version: Mapped[str | None] = mapped_column(String(32))
    api_version: Mapped[str | None] = mapped_column(String(32))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    # JSON list of the recent failed API calls the browser saw: path, status and
    # the server's own message. This is the field that usually answers it.
    api_errors_json: Mapped[str | None] = mapped_column(Text)


BUG_SEVERITIES = ("blocker", "high", "normal", "low")
BUG_SOURCES = ("manual", "server", "browser")
BUG_STATUSES = ("open", "fixed", "wontfix")


class PortalFinding(Base):
    """One thing a portal said about one property, waiting to be looked at.

    The portal pass used to write as it went. It now records what it found and
    changes nothing until a person says so, because a figure scraped off someone
    else's page is a claim, not a fact — and the moment it lands in a priced
    field it is indistinguishable from data we stand behind.

    A finding is one field from one source: "OneRoof says the floor area is
    185m²", "Trade Me says it is worth $1.24M". Approve it and it is written and
    the listing re-priced through the usual rules; reject it and it stays here as
    a record of what was offered and refused, so the same wrong number does not
    come back next week looking new.
    """
    __tablename__ = "portal_findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(index=True, nullable=False)
    batch_id: Mapped[int | None] = mapped_column(index=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    # Our own column name for a fact ("floor_area_m2"), or the portal's estimate
    # column ("tm_valuation") for an estimate.
    field: Mapped[str] = mapped_column(String(48), nullable=False)
    # "fact" changes what the property is worth and re-prices it on approval.
    # "estimate" is that portal's opinion, shown as theirs, never an input.
    kind: Mapped[str] = mapped_column(String(8), default="fact", nullable=False)
    value_num: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(String(255))
    # What we hold right now — normally nothing, which is why it was offered.
    current_num: Mapped[float | None] = mapped_column(Float)
    current_text: Mapped[str | None] = mapped_column(String(255))
    extra_json: Mapped[str | None] = mapped_column(Text)   # low/high/url
    status: Mapped[str] = mapped_column(String(12), default="pending",
                                        nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        Index("ix_finding_pending", "status", "property_id"),
    )


class PortalListing(Base):
    """A property a portal is advertising that our own feed has not reached yet.

    The weekly file is a snapshot: a home listed on Tuesday appears in it the
    following Monday. For a deal-finding product that is the whole game — an
    underpriced listing is under offer inside a week, so six days late is the
    difference between seeing it and reading about it.

    So the portals are swept daily for what was published in the last 24 hours,
    and anything we do not already hold lands here. Nothing is live until a
    person says so: this is a CLAIM about a property, scraped off someone else's
    page, and the moment it becomes a row in the live batch it is
    indistinguishable from data we stand behind.

    Deliberately NOT a PropertyForSale. A for-sale batch is a snapshot of one
    week and only one is ever live — "an old week is not on the market" — so
    daily arrivals cannot be another batch without breaking that. They wait here
    and are merged into the live batch on approval.

    What no portal carries: `zoning` and `type_of_title`. Those are council and
    LINZ records, not listing data, and both gate the subdivision engine. An
    approved row prices normally and reads as "not subdividable" until the
    weekly file catches up and fills them in — which is a known gap, not a
    verdict about the property.
    """
    __tablename__ = "portal_listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(500))
    # "for_sale" (daily) or "sold" (weekly). One table because everything about
    # them is the same — a scraped claim about a property, deduped by address,
    # waiting for someone to agree — and they differ only in the three columns
    # below and which table they become on approval.
    kind: Mapped[str] = mapped_column(String(12), default="for_sale",
                                      nullable=False, index=True)

    # The dedupe key: app.trademe.address_key, so "3/107 Donovan Street" and
    # "3 / 107 Donovan St" are one property however each portal spells it.
    address: Mapped[str | None] = mapped_column(String(255))
    address_key: Mapped[str | None] = mapped_column(String(255), index=True)
    suburb: Mapped[str | None] = mapped_column(String(120), index=True)
    district: Mapped[str | None] = mapped_column(String(120))
    property_type: Mapped[str | None] = mapped_column(String(60))

    price_numeric: Mapped[float | None] = mapped_column(Float)
    price_display: Mapped[str | None] = mapped_column(String(255))
    cv_numeric: Mapped[float | None] = mapped_column(Float)
    land_value_numeric: Mapped[float | None] = mapped_column(Float)
    improvement_value_numeric: Mapped[float | None] = mapped_column(Float)
    floor_area_m2: Mapped[float | None] = mapped_column(Float)
    land_area_m2: Mapped[float | None] = mapped_column(Float)
    beds: Mapped[float | None] = mapped_column(Float)
    baths: Mapped[float | None] = mapped_column(Float)
    carspaces: Mapped[float | None] = mapped_column(Float)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    image_url: Mapped[str | None] = mapped_column(String(500))
    listed_date: Mapped[str | None] = mapped_column(String(32))

    # --- sold only ---------------------------------------------------------
    # A wrong asking price costs one listing. A wrong SALE price poisons the
    # comps for a whole suburb — it feeds the sale/CV ratio, the $/m2 rate and
    # every valuation that leans on them — which is why these arrive with a
    # sanity check against the suburb's own range before anyone sees them.
    sale_price: Mapped[float | None] = mapped_column(Float)
    sold_date: Mapped[str | None] = mapped_column(String(32))
    sale_method: Mapped[str | None] = mapped_column(String(64))
    days_on_market: Mapped[float | None] = mapped_column(Float)
    # Set when a sale price sits outside what the suburb has been doing. Not a
    # rejection — an unusual sale is often real — but it is the row to read
    # before pressing approve.
    price_flag: Mapped[str | None] = mapped_column(String(48))

    # --- what only the richer OneRoof actor supplies ------------------------
    # Zoning and title were the two fields I had said no portal publishes, and
    # both gate the subdivision engine. fatihtahta/oneroof-nz-scraper carries
    # them as property_data["Unitary Plan"] and property_data["Title"], which is
    # the difference between a portal listing that prices like any other and one
    # that reads "not subdividable" whatever the site is.
    zoning: Mapped[str | None] = mapped_column(String(120))
    type_of_title: Mapped[str | None] = mapped_column(String(60))
    building_age: Mapped[str | None] = mapped_column(String(32))
    condition: Mapped[str | None] = mapped_column(String(60))
    # That portal's OWN estimate. Stored as theirs, shown as theirs, and never
    # an input to our valuation — see the note in app/portals/fill.py.
    estimate: Mapped[float | None] = mapped_column(Float)
    estimate_low: Mapped[float | None] = mapped_column(Float)
    estimate_high: Mapped[float | None] = mapped_column(Float)
    last_sold_price: Mapped[float | None] = mapped_column(Float)
    last_sold_date: Mapped[str | None] = mapped_column(String(32))

    # Kept because detect_pool() reads it, and because a description is the only
    # place a portal says anything about condition.
    description: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(12), default="pending",
                                        nullable=False, index=True)
    # Set when approved: which PropertyForSale this became.
    property_id: Mapped[int | None] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        Index("ix_portal_listing_pending", "status", "kind", "suburb"),
        # One row per property per portal per kind, however often the sweep
        # runs — a house can legitimately appear once for sale and once sold.
        Index("ix_portal_listing_dedupe", "source", "kind", "address_key"),
    )
