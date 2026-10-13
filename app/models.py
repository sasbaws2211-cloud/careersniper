import secrets
from datetime import datetime

from pydantic import NaiveDatetime
from sqlalchemy import JSON, Column, ForeignKey, Integer, LargeBinary, Text, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlmodel import Field, Relationship, SQLModel

from . import config


def _now():
    return datetime.utcnow()


def _token():
    return secrets.token_urlsafe(24)


# All tables are SQLModel `table=True` classes. Relationships use eager loading (joined/selectin) because lazy loads
# cannot run inside an AsyncSession: anything a handler or template reads off a row must already be loaded.

class PlatformConfig(SQLModel, table=True):
    """Singleton row holding global platform settings (branding, defaults, feature flags)."""
    __tablename__ = "platform_config"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(default="CareerSniper", max_length=120)
    tagline: str = Field(default="Jobs posted directly on company career pages", max_length=200)
    logo_url: str = Field(default="", max_length=500)
    brand_color: str = Field(default="#2563eb", max_length=7)
    default_max_job_age_days: int = 90
    scraping_enabled: bool = True
    ai_enabled: bool = False
    billing_enabled: bool = False
    created_at: NaiveDatetime = Field(default_factory=_now)
    updated_at: NaiveDatetime = Field(default_factory=_now, sa_column_kwargs={"onupdate": _now})

    @property
    def limits(self) -> dict:
        """Global limits for the platform (used for free vs paid users)."""
        return config.PLANS.get("free", {})


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(max_length=255, unique=True, index=True)
    password_hash: str = Field(max_length=255)
    role: str = Field(default="member", max_length=20)
    # users <-> subscriptions reference each other: a named, deferred (use_alter) key lets create_all/drop_all order the tables
    subscription_id: int | None = Field(default=None, sa_column=Column(Integer, ForeignKey(
        "subscriptions.id", use_alter=True, name="users_subscription_id_fkey"), index=True, nullable=True))
    created_at: NaiveDatetime = Field(default_factory=_now)

    subscription: "Subscription | None" = Relationship(sa_relationship=relationship(
        "Subscription",
        foreign_keys="User.subscription_id",
        primaryjoin="User.subscription_id==Subscription.id",
        uselist=False,
        viewonly=True,
        lazy="joined",
    ))

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active_subscriber(self) -> bool:
        if not self.subscription:
            return False
        return self.subscription.status == "active" and self.subscription.current_period_end > _now()


class Company(SQLModel, table=True):
    """A career-page source. Part of the shared platform registry."""
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("ats", "board_token", name="uq_company_board"),)
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=200)
    ats: str = Field(max_length=30)
    board_token: str = Field(max_length=120)
    active: bool = True
    last_scraped: NaiveDatetime | None = None
    last_error: str = Field(default="", max_length=300)
    job_count: int = 0
    trusted: bool | None = False
    website: str | None = Field(default=None, max_length=300)
    logo_version: int | None = None
    free_posting: bool | None = False
    credits: int | None = 0


class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("company_id", "external_id", name="uq_job_ext"),
        Index("ix_jobs_active_seen", "active", "first_seen"),
    )
    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    external_id: str = Field(max_length=120)
    title: str = Field(max_length=300)
    company_name: str = Field(default="", max_length=200)
    dup_key: str | None = Field(default=None, max_length=40, index=True)
    location: str = Field(default="", max_length=300)
    remote: bool = False
    department: str = Field(default="", max_length=200)
    employment_type: str = Field(default="", max_length=60)
    url: str = Field(max_length=1000)
    description: str = Field(default="", sa_type=Text)
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = Field(default="", max_length=8)
    salary_period: str | None = Field(default=None, max_length=10)
    pay_min_usd: int | None = None
    pay_max_usd: int | None = None
    countries: str | None = Field(default=None, max_length=200)
    job_type: str | None = Field(default=None, max_length=20)
    seniority: str | None = Field(default=None, max_length=20)
    source: str | None = Field(default=None, max_length=8)
    status: str | None = Field(default=None, max_length=10)
    expires_at: NaiveDatetime | None = None
    featured_until: NaiveDatetime | None = None
    submitted_at: NaiveDatetime | None = None
    review_note: str | None = Field(default=None, max_length=500)
    created_by: int | None = None
    feature_requested_at: NaiveDatetime | None = None
    feature_request_days: int | None = None
    paid_feature_days: int | None = None
    expiry_notice_sent: bool | None = None
    workplace: str | None = Field(default=None, max_length=10)
    remote_regions: str | None = Field(default=None, max_length=200)
    visa: str | None = Field(default=None, max_length=4)
    posted_at: NaiveDatetime | None = None
    first_seen: NaiveDatetime = Field(default_factory=_now)
    last_seen: NaiveDatetime = Field(default_factory=_now)
    active: bool = True
    # how many times this role was closed and then posted again (same employer, title and location); a high count hints at an evergreen listing
    repost_count: int | None = 0

    company: "Company" = Relationship(sa_relationship=relationship("Company", lazy="joined"))


class ClickEvent(SQLModel, table=True):
    """Apply-click analytics."""
    __tablename__ = "click_events"
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    created_at: NaiveDatetime = Field(default_factory=_now, index=True)


class BillingProviderPlan(SQLModel, table=True):
    """Flutterwave payment-plan ids, created lazily per (plan, interval, currency, price)."""
    __tablename__ = "billing_provider_plans"
    __table_args__ = (UniqueConstraint("plan", "interval", "currency", "amount_cents", name="uq_provider_plan"),)
    id: int | None = Field(default=None, primary_key=True)
    plan: str = Field(max_length=20)
    interval: str = Field(max_length=10)
    currency: str = Field(max_length=8)
    amount_cents: int
    provider_plan_id: str = Field(max_length=60)


class BillingCheckout(SQLModel, table=True):
    __tablename__ = "billing_checkouts"
    id: int | None = Field(default=None, primary_key=True)
    tx_ref: str = Field(max_length=80, unique=True)
    plan: str = Field(max_length=20)
    interval: str = Field(max_length=10)
    amount_cents: int
    currency: str = Field(max_length=8)
    provider_plan_id: str = Field(max_length=60)
    customer_email: str = Field(max_length=255)
    checkout_url: str = Field(default="", max_length=1000)
    status: str = Field(default="pending", max_length=12)
    provider_tx_id: str = Field(default="", max_length=60)
    created_at: NaiveDatetime = Field(default_factory=_now)
    completed_at: NaiveDatetime | None = None


class Subscription(SQLModel, table=True):
    """User subscription (B2C)."""
    __tablename__ = "subscriptions"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="users.id", unique=True, index=True)
    plan: str = Field(max_length=20)
    interval: str = Field(max_length=10)
    amount_cents: int
    currency: str = Field(max_length=8)
    status: str = Field(default="active", max_length=12)
    customer_email: str = Field(max_length=255)
    provider_plan_id: str | None = Field(default=None, max_length=60)
    provider_subscription_id: str | None = Field(default=None, max_length=60)
    email_token: str | None = Field(default=None, max_length=80)   # Paystack needs it to disable a subscription
    current_period_start: NaiveDatetime
    current_period_end: NaiveDatetime
    last_payment_at: NaiveDatetime | None = None
    cancelled_at: NaiveDatetime | None = None
    created_at: NaiveDatetime = Field(default_factory=_now)

    user: "User | None" = Relationship(sa_relationship=relationship(
        "User", foreign_keys="Subscription.user_id", primaryjoin="Subscription.user_id==User.id", lazy="selectin"))


class BillingEvent(SQLModel, table=True):
    """One row per provider event we applied; the unique key makes webhook retries no-ops."""
    __tablename__ = "billing_events"
    id: int | None = Field(default=None, primary_key=True)
    event_key: str = Field(max_length=120, unique=True)
    event_type: str = Field(max_length=40)
    outcome: str = Field(max_length=200)
    payload: dict = Field(default_factory=dict, sa_type=JSON)
    created_at: NaiveDatetime = Field(default_factory=_now)


class Subscriber(SQLModel, table=True):
    """A user's saved search: emailed digests of new matching jobs (double opt-in)."""
    __tablename__ = "subscribers"
    __table_args__ = (Index("ix_sub_email", "email"),)
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(max_length=255)
    q: str = Field(default="", max_length=200)
    loc: str = Field(default="", max_length=200)
    remote: bool = False
    filters: str | None = Field(default="", max_length=200)
    frequency: str = Field(default="daily", max_length=10)
    token: str = Field(default_factory=_token, max_length=64, unique=True)
    confirmed: bool = False
    active: bool = True
    confirm_sent_at: NaiveDatetime | None = None
    last_checked_at: NaiveDatetime | None = None
    last_sent_at: NaiveDatetime | None = None
    digests_sent: int = 0
    created_at: NaiveDatetime = Field(default_factory=_now)


class Member(SQLModel, table=True):
    """A job seeker's account (global, not per-tenant)."""
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("email", name="uq_member_email"),)
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(max_length=255)
    password_hash: str = Field(max_length=255)
    verified: bool = False
    created_at: NaiveDatetime = Field(default_factory=_now)


class MemberProfile(SQLModel, table=True):
    """Preferences collected during first-time onboarding.
    Used to pre-filter the job board, seed alerts, and personalise the pitch.
    """
    __tablename__ = "member_profiles"
    __table_args__ = (UniqueConstraint("member_id", name="uq_member_profile"),)
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="members.id", index=True)
    employment_status: str = Field(default="", max_length=40)
    desired_titles: list = Field(default_factory=list, sa_type=JSON)
    experience_level: str = Field(default="", max_length=20)
    salary_min: int | None = Field(default=None)
    job_types: list = Field(default_factory=list, sa_type=JSON)
    work_country: str = Field(default="", max_length=60)
    remote_preference: str = Field(default="", max_length=20)
    looking_duration: str = Field(default="", max_length=30)
    main_frustration: str = Field(default="", max_length=40)
    has_resume: bool | None = Field(default=None)
    tailors_resume: bool | None = Field(default=None)
    completed_at: NaiveDatetime | None = None
    created_at: NaiveDatetime = Field(default_factory=_now)
    updated_at: NaiveDatetime = Field(default_factory=_now, sa_column_kwargs={"onupdate": _now})


class SavedJob(SQLModel, table=True):
    __tablename__ = "saved_jobs"
    __table_args__ = (UniqueConstraint("member_id", "job_id", name="uq_saved_member_job"),)
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="members.id", index=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    status: str = Field(default="saved", max_length=10)
    created_at: NaiveDatetime = Field(default_factory=_now)


class AiUsage(SQLModel, table=True):
    """One row per successful AI call: drives the plan's monthly credits and the per-member daily cap."""
    __tablename__ = "ai_usage"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    member_id: int | None = Field(default=None, foreign_key="members.id", index=True)
    tool: str = Field(max_length=30)
    model: str = Field(default="", max_length=60)
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: NaiveDatetime = Field(default_factory=_now, index=True)


class Resume(SQLModel, table=True):
    __tablename__ = "resumes"
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="members.id", index=True)
    title: str = Field(default="My resume", max_length=120)
    template: str = Field(default="classic", max_length=20)
    data: dict = Field(default_factory=dict, sa_type=JSON)
    created_at: NaiveDatetime = Field(default_factory=_now)
    updated_at: NaiveDatetime = Field(default_factory=_now, sa_column_kwargs={"onupdate": _now})


class Document(SQLModel, table=True):
    """Generated writing the member keeps: cover letters and follow-up emails."""
    __tablename__ = "documents"
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="members.id", index=True)
    kind: str = Field(max_length=20)
    title: str = Field(default="", max_length=200)
    content: str = Field(default="", sa_type=Text)
    job_id: int | None = Field(default=None, foreign_key="jobs.id")
    meta: dict | None = Field(default=None, sa_type=JSON)      # e.g. what a tailored résumé changed and what it could not cover
    created_at: NaiveDatetime = Field(default_factory=_now)


class InterviewSession(SQLModel, table=True):
    __tablename__ = "interview_sessions"
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="members.id", index=True)
    job_id: int | None = Field(default=None, foreign_key="jobs.id")
    role: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    questions: list = Field(default_factory=list, sa_type=JSON)
    resume_id: int | None = None
    created_at: NaiveDatetime = Field(default_factory=_now)


class Application(SQLModel, table=True):
    """A job the member is pursuing (on this board or anywhere), tracked from applied to offer."""
    __tablename__ = "applications"
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="members.id", index=True)
    job_id: int | None = Field(default=None, foreign_key="jobs.id")
    company: str = Field(max_length=200)
    title: str = Field(max_length=300)
    url: str = Field(default="", max_length=1000)
    status: str = Field(default="applied", max_length=15)
    notes: str = Field(default="", sa_type=Text)
    applied_on: NaiveDatetime | None = None
    created_at: NaiveDatetime = Field(default_factory=_now)
    updated_at: NaiveDatetime = Field(default_factory=_now, sa_column_kwargs={"onupdate": _now})


class JobReport(SQLModel, table=True):
    """A member saying a listing is closed or dead. Enough distinct reports hide the job (see app/reports.py)."""
    __tablename__ = "job_reports"
    __table_args__ = (UniqueConstraint("job_id", "member_id", name="uq_job_report_member"),)
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    member_id: int = Field(foreign_key="members.id", index=True)
    created_at: NaiveDatetime = Field(default_factory=_now)


class Invite(SQLModel, table=True):
    """Admin invite (for team/admin management, not per-tenant)."""
    __tablename__ = "invites"
    __table_args__ = (Index("ix_invite_email", "email"),)
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(max_length=255)
    token: str = Field(default_factory=_token, max_length=64, unique=True)
    invited_by: int | None = None
    created_at: NaiveDatetime = Field(default_factory=_now)
    expires_at: NaiveDatetime
    last_sent_at: NaiveDatetime | None = None
    status: str = Field(default="pending", max_length=10)


class JobRequirement(SQLModel, table=True):
    """What one job posting asks for, analysed once (app/jobreq.py) and shared by everyone who tailors a résumé to it."""
    __tablename__ = "job_requirements"
    job_id: int = Field(foreign_key="jobs.id", primary_key=True)
    data: dict = Field(default_factory=dict, sa_type=JSON)
    created_at: NaiveDatetime = Field(default_factory=_now)


class CompanyLogo(SQLModel, table=True):
    """A direct employer's logo."""
    __tablename__ = "company_logos"
    company_id: int = Field(foreign_key="companies.id", primary_key=True)
    content_type: str = Field(max_length=20)
    data: bytes = Field(sa_type=LargeBinary)
    updated_at: NaiveDatetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Compatibility shim during B2C pivot (Org was removed by migration).
# Code that still expects Org gets a lightweight stand-in representing the
# single global platform. Do not persist new Org rows.
# ---------------------------------------------------------------------------
class Org:
    """In-memory stand-in for the removed multi-tenant Org model.
    Represents the single B2C platform. Not a mapped SQLAlchemy model.
    """
    id = 0
    slug = "platform"
    name = "CareerSniper"
    plan = "lifetime"
    custom_domain = None
    custom_domain_verified = False
    hide_branding = True
    only_own_companies = False
    include_keywords = ""
    exclude_keywords = ""
    remote_only = False
    job_age_limit = None
    logo_url = ""
    brand_color = "#2563eb"
    tagline = "Find jobs not on LinkedIn/Indeed"
    ai_enabled = True
    tools_disabled = ""
    employer_posting = False

    @property
    def limits(self):
        from . import config
        base = dict(config.PLANS.get("lifetime", config.PLANS.get("free", {})))
        base.setdefault("remove_branding", True)
        base.setdefault("custom_domain", False)
        base.setdefault("seats", 1)
        base.setdefault("api", False)
        base.setdefault("own_companies", 0)
        return base

    @property
    def effective_plan(self):
        return "lifetime"

PLATFORM_ORG = Org()


# Stubs for employer-portal / direct-posting modules that still import these names.
# Direct employer posting is secondary in the B2C product; keep schema migrations intact.
class EmployerUser:
    pass

class EmployerOrder:
    pass

class CreditPackage:
    pass

class CreditLot:
    pass

class EmployerPlan:
    pass

class EmployerSubscription:
    pass

class CreditEntry:
    pass



class CompanyFollow(SQLModel, table=True):
    """A member follows an employer (stored against the company's canonical record)."""
    __tablename__ = "company_follows"
    __table_args__ = (UniqueConstraint("member_id", "company_id", name="uq_follow"),)
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="members.id", index=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    created_at: NaiveDatetime = Field(default_factory=_now)


class FollowDigest(SQLModel, table=True):
    """Whether, and since when, a member gets the weekly digest of new roles at the companies they follow."""
    __tablename__ = "follow_digests"
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="members.id", unique=True, index=True)
    enabled: bool = True
    last_checked_at: NaiveDatetime | None = None
    last_sent_at: NaiveDatetime | None = None
    digests_sent: int = 0


class ReferralCode(SQLModel, table=True):
    """A member's personal invite code (the part after /r/)."""
    __tablename__ = "referral_codes"
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="members.id", unique=True, index=True)
    code: str = Field(max_length=12, unique=True, index=True)


class Referral(SQLModel, table=True):
    """One friend who signed up through a member's link, and what became of the reward."""
    __tablename__ = "referrals"
    id: int | None = Field(default=None, primary_key=True)
    referrer_id: int = Field(foreign_key="members.id", index=True)
    referred_id: int = Field(foreign_key="members.id", unique=True, index=True)
    code: str = Field(max_length=12)
    status: str = Field(default="signed_up", max_length=12)      # signed_up | rewarded | withdrawn | capped | skipped
    reward_days: int = 0
    reward_sub_id: int | None = None
    reward_created_sub: bool = False   # True: the reward was a new free-week subscription
    created_at: NaiveDatetime = Field(default_factory=_now)
    rewarded_at: NaiveDatetime | None = None


class AdminAudit(SQLModel, table=True):
    """What a platform admin did, to whom, and from where. Written by the standalone admin app; never edited or deleted by it."""
    __tablename__ = "admin_audit"
    id: int | None = Field(default=None, primary_key=True)
    admin_email: str = Field(max_length=255, index=True)
    action: str = Field(max_length=40, index=True)
    target: str = Field(default="", max_length=200)
    detail: dict | None = Field(default=None, sa_type=JSON)
    ip: str = Field(default="", max_length=64)
    created_at: NaiveDatetime = Field(default_factory=_now, index=True)


class PlatformSetting(SQLModel, table=True):
    """One platform setting an admin changed in the admin app. A key that is absent falls back to its environment variable."""
    __tablename__ = "platform_settings"
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(max_length=60, unique=True, index=True)
    value: dict | list | str | int | float | bool | None = Field(default=None, sa_type=JSON)     # a secret is {"enc": "<encrypted>"}
    updated_by: str = Field(default="", max_length=255)
    updated_at: NaiveDatetime = Field(default_factory=_now)
