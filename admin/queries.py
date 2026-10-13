"""Everything the admin pages read. Read-only: nothing here changes the database."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ai, runtime_settings
from app import config as site
from app.models import (AdminAudit, AiUsage, Application, BillingCheckout, BillingEvent, Company, CompanyFollow, Document, InterviewSession, Job, Member,
                        Referral, ReferralCode, Resume, Subscriber, Subscription, User)

PER_PAGE = 25
LIVE = ("active", "cancelled")                 # a cancelled plan still runs until its period ends


def _now() -> datetime:
    return datetime.utcnow()


def pages(total: int, per: int | None = None) -> int:
    return max(1, -(-total // (per or PER_PAGE)))


def _live_subs():
    return select(Subscription).where(Subscription.status.in_(LIVE), Subscription.current_period_end > _now())


def _like(text: str) -> str:
    return "%" + (text or "").strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _days(rows, field, days: int, now: datetime) -> list[dict]:
    """[{label, value}] for each of the last `days` days, oldest first, counting rows whose `field` falls on that day."""
    counts = Counter()
    for r in rows:
        t = getattr(r, field) if not isinstance(r, datetime) else r
        if t:
            counts[t.date()] += 1
    out = []
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).date()
        out.append({"label": d.strftime("%d %b"), "value": counts.get(d, 0)})
    return out


# ---------------------------------------------------------------- dashboard
async def dashboard(db: AsyncSession, now: datetime | None = None) -> dict:
    now = now or _now()
    day, week, month = now - timedelta(days=1), now - timedelta(days=7), now - timedelta(days=30)
    async def count(stmt):
        return (await db.scalar(stmt)) or 0

    members_total = await count(select(func.count()).select_from(Member))
    signups = (await db.scalars(select(Member.created_at).where(Member.created_at >= now - timedelta(days=14)))).all()
    live = (await db.scalars(_live_subs())).all()
    plans = Counter(("comped" if s.amount_cents == 0 else s.plan) for s in live)
    paid_30 = (await db.execute(select(func.count(), func.coalesce(func.sum(BillingCheckout.amount_cents), 0)).where(
        BillingCheckout.status == "paid", BillingCheckout.completed_at >= month))).one()
    paid_rows = (await db.scalars(select(BillingCheckout.completed_at).where(BillingCheckout.status == "paid", BillingCheckout.completed_at >= now - timedelta(days=14)))).all()
    ghs = paid_30[1] / 100
    ai_today = (await db.execute(select(func.count(), func.coalesce(func.sum(AiUsage.input_tokens + AiUsage.output_tokens), 0)).where(AiUsage.created_at >= day))).one()
    ai_rows = (await db.scalars(select(AiUsage.created_at).where(AiUsage.created_at >= now - timedelta(days=14)))).all()
    active_cos = await count(select(func.count()).select_from(Company).where(Company.active.is_(True)))
    last_scrape = await db.scalar(select(func.max(Company.last_scraped)).where(Company.active.is_(True)))

    return {
        "members": {"total": members_total, "new_7d": (await count(select(func.count()).select_from(Member).where(Member.created_at >= week))),
                    "unverified": (await count(select(func.count()).select_from(Member).where(Member.verified.is_(False))))},
        "plans": dict(sorted(plans.items())), "live_plans": len(live),
        "revenue": {"payments_30d": paid_30[0], "ghs_30d": ghs, "usd_30d": ghs / site.USD_TO_GHS if site.USD_TO_GHS else 0,
                    "failed_7d": (await count(select(func.count()).select_from(BillingCheckout).where(BillingCheckout.status == "failed", BillingCheckout.completed_at >= week))),
                    "stuck": (await count(select(func.count()).select_from(BillingCheckout).where(BillingCheckout.status == "pending", BillingCheckout.created_at < now - timedelta(hours=1),
                                                                                           BillingCheckout.created_at >= week)))},
        "jobs": {"live": (await count(select(func.count()).select_from(Job).join(Company).where(Job.active.is_(True), Company.active.is_(True),
                                                                                           or_(Job.status.is_(None), Job.status == "live")))),
                 "removed": (await count(select(func.count()).select_from(Job).where(Job.status == "removed")))},
        "scrape": {"companies": active_cos, "errors": (await count(select(func.count()).select_from(Company).where(Company.active.is_(True), Company.last_error != ""))),
                   "never": (await count(select(func.count()).select_from(Company).where(Company.active.is_(True), Company.last_scraped.is_(None)))),
                   "stale": (await count(select(func.count()).select_from(Company).where(Company.active.is_(True), Company.last_scraped < now - timedelta(hours=12)))),
                   "last": last_scrape},
        "ai": {"calls_today": ai_today[0], "tokens_today": int(ai_today[1] or 0)},
        "engagement": {"alerts": (await count(select(func.count()).select_from(Subscriber).where(Subscriber.active.is_(True), Subscriber.confirmed.is_(True)))),
                       "follows": (await count(select(func.count()).select_from(CompanyFollow))), "applications": (await count(select(func.count()).select_from(Application))),
                       "documents": (await count(select(func.count()).select_from(Document))), "interviews": (await count(select(func.count()).select_from(InterviewSession))),
                       "resumes": (await count(select(func.count()).select_from(Resume))),
                       "referrals": (await count(select(func.count()).select_from(Referral))),
                       "rewarded": (await count(select(func.count()).select_from(Referral).where(Referral.status == "rewarded")))},
        "signup_series": _days(signups, "created_at", 14, now),
        "payment_series": _days(paid_rows, "x", 14, now), "ai_series": _days(ai_rows, "x", 14, now),
        "recent_members": (await db.execute(select(Member.id, Member.email, Member.verified, Member.created_at).order_by(Member.id.desc()).limit(8))).all(),
        "recent_payments": (await db.execute(select(BillingCheckout.customer_email, BillingCheckout.plan, BillingCheckout.amount_cents, BillingCheckout.currency,
                                             BillingCheckout.status, BillingCheckout.completed_at, BillingCheckout.created_at)
                                      .order_by(BillingCheckout.id.desc()).limit(8))).all(),
        "recent_audit": (await db.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).limit(8))).all(),
    }


# ---------------------------------------------------------------- members
async def current_plan(db: AsyncSession, email: str) -> dict | None:
    s = await db.scalar(select(Subscription).where(Subscription.customer_email == email.lower(), Subscription.status.in_(LIVE), Subscription.current_period_end > _now())
                  .order_by(Subscription.id.desc()))
    return {"plan": s.plan, "ends": s.current_period_end, "comped": s.amount_cents == 0, "status": s.status} if s else None


async def members_page(db: AsyncSession, q: str = "", verified: str = "", plan: str = "", page: int = 1) -> tuple[int, list[dict]]:
    stmt = select(Member)
    if q.strip():
        stmt = stmt.where(func.lower(Member.email).like(_like(q), escape="\\"))
    if verified in ("yes", "no"):
        stmt = stmt.where(Member.verified.is_(verified == "yes"))
    paying = select(Subscription.customer_email).where(Subscription.status.in_(LIVE), Subscription.current_period_end > _now())
    if plan == "paid":
        stmt = stmt.where(func.lower(Member.email).in_(paying))
    elif plan == "free":
        stmt = stmt.where(func.lower(Member.email).not_in(paying))
    total = (await db.scalar(select(func.count()).select_from(stmt.subquery()))) or 0
    rows = (await db.scalars(stmt.order_by(Member.id.desc()).offset((max(1, page) - 1) * PER_PAGE).limit(PER_PAGE))).all()
    return total, [{"member": m, "plan": (await current_plan(db, m.email))} for m in rows]


async def member_detail(db: AsyncSession, member_id: int) -> dict | None:
    m = await db.get(Member, member_id)
    if not m:
        return None
    email = m.email.lower()
    subs = (await db.scalars(select(Subscription).where(Subscription.customer_email == email).order_by(Subscription.id.desc()).limit(20))).all()
    checkouts = (await db.scalars(select(BillingCheckout).where(BillingCheckout.customer_email == email).order_by(BillingCheckout.id.desc()).limit(20))).all()
    ai = (await db.execute(select(AiUsage.tool, func.count(), func.coalesce(func.sum(AiUsage.input_tokens + AiUsage.output_tokens), 0)).where(AiUsage.member_id == m.id)
                    .group_by(AiUsage.tool).order_by(func.count().desc()))).all()
    code = await db.scalar(select(ReferralCode.code).where(ReferralCode.member_id == m.id))
    referred_by = (await db.execute(select(Member.email, Referral.status).join(Referral, Referral.referrer_id == Member.id).where(Referral.referred_id == m.id))).first()
    referred = (await db.execute(select(Member.id, Member.email, Referral.status).join(Referral, Referral.referred_id == Member.id).where(Referral.referrer_id == m.id))).all()
    counts = {k: (await db.scalar(select(func.count()).select_from(model).where(col == m.id))) or 0 for k, (model, col) in {
        "applications": (Application, Application.member_id), "documents": (Document, Document.member_id), "interviews": (InterviewSession, InterviewSession.member_id),
        "follows": (CompanyFollow, CompanyFollow.member_id)}.items()}
    counts["alerts"] = (await db.scalar(select(func.count()).select_from(Subscriber).where(func.lower(Subscriber.email) == email, Subscriber.active.is_(True)))) or 0
    counts["resume"] = bool((await db.scalar(select(func.count()).select_from(Resume).where(Resume.member_id == m.id))))
    return {"member": m, "plan": (await current_plan(db, email)), "subs": subs, "checkouts": checkouts, "ai": ai, "ref_code": code, "referred_by": referred_by,
            "referred": referred, "counts": counts}


# ---------------------------------------------------------------- billing
async def payments_page(db: AsyncSession, status: str = "", page: int = 1) -> tuple[int, list]:
    stmt = select(BillingCheckout)
    if status in ("paid", "failed", "pending"):
        stmt = stmt.where(BillingCheckout.status == status)
    total = (await db.scalar(select(func.count()).select_from(stmt.subquery()))) or 0
    return total, (await db.scalars(stmt.order_by(BillingCheckout.id.desc()).offset((max(1, page) - 1) * PER_PAGE).limit(PER_PAGE))).all()


async def subscriptions_page(db: AsyncSession, state: str = "live", page: int = 1) -> tuple[int, list]:
    stmt = select(Subscription)
    if state == "live":
        stmt = stmt.where(Subscription.status.in_(LIVE), Subscription.current_period_end > _now())
    elif state == "ended":
        stmt = stmt.where(or_(Subscription.status.notin_(LIVE), Subscription.current_period_end <= _now()))
    total = (await db.scalar(select(func.count()).select_from(stmt.subquery()))) or 0
    return total, (await db.scalars(stmt.order_by(Subscription.id.desc()).offset((max(1, page) - 1) * PER_PAGE).limit(PER_PAGE))).all()


async def events_page(db: AsyncSession, page: int = 1) -> tuple[int, list]:
    total = (await db.scalar(select(func.count()).select_from(BillingEvent))) or 0
    return total, (await db.scalars(select(BillingEvent).order_by(BillingEvent.id.desc()).offset((max(1, page) - 1) * PER_PAGE).limit(PER_PAGE))).all()


async def member_ids_by_email(db: AsyncSession, emails: list[str]) -> dict[str, int]:
    if not emails:
        return {}
    return {e.lower(): i for i, e in (await db.execute(select(Member.id, Member.email).where(func.lower(Member.email).in_([x.lower() for x in emails]))))}


# ---------------------------------------------------------------- companies and jobs
async def companies_page(db: AsyncSession, q: str = "", flt: str = "", page: int = 1) -> tuple[int, list[dict]]:
    now = _now()
    stmt = select(Company)
    if q.strip():
        stmt = stmt.where(or_(func.lower(Company.name).like(_like(q), escape="\\"), func.lower(Company.board_token).like(_like(q), escape="\\")))
    if flt == "errors":
        stmt = stmt.where(Company.active.is_(True), Company.last_error != "")
    elif flt == "inactive":
        stmt = stmt.where(Company.active.is_(False))
    elif flt == "never":
        stmt = stmt.where(Company.active.is_(True), Company.last_scraped.is_(None))
    elif flt == "stale":
        stmt = stmt.where(Company.active.is_(True), Company.last_scraped < now - timedelta(hours=12))
    total = (await db.scalar(select(func.count()).select_from(stmt.subquery()))) or 0
    rows = (await db.scalars(stmt.order_by(Company.name, Company.id).offset((max(1, page) - 1) * PER_PAGE).limit(PER_PAGE))).all()
    ids = [c.id for c in rows]
    live = dict((await db.execute(select(Job.company_id, func.count()).where(Job.company_id.in_(ids), Job.active.is_(True)).group_by(Job.company_id))).all()) if ids else {}
    return total, [{"company": c, "live": live.get(c.id, 0)} for c in rows]


async def jobs_page(db: AsyncSession, q: str = "", state: str = "live", page: int = 1) -> tuple[int, list[dict]]:
    stmt = select(Job, Company.name).join(Company, Company.id == Job.company_id)
    if q.strip():
        stmt = stmt.where(or_(func.lower(Job.title).like(_like(q), escape="\\"), func.lower(Company.name).like(_like(q), escape="\\")))
    if state == "live":
        stmt = stmt.where(Job.active.is_(True), or_(Job.status.is_(None), Job.status == "live"))
    elif state == "removed":
        stmt = stmt.where(Job.status == "removed")
    elif state == "closed":
        stmt = stmt.where(Job.active.is_(False))
    total = (await db.scalar(select(func.count()).select_from(stmt.subquery()))) or 0
    rows = (await db.execute(stmt.order_by(Job.id.desc()).offset((max(1, page) - 1) * PER_PAGE).limit(PER_PAGE))).all()
    return total, [{"job": j, "company": name} for j, name in rows]


# ---------------------------------------------------------------- AI, referrals, audit, admins, system
async def ai_report(db: AsyncSession, now: datetime | None = None) -> dict:
    now = now or _now()
    per_tool = (await db.execute(select(AiUsage.tool, func.count(), func.coalesce(func.sum(AiUsage.input_tokens), 0), func.coalesce(func.sum(AiUsage.output_tokens), 0))
                          .where(AiUsage.created_at >= now - timedelta(days=30)).group_by(AiUsage.tool).order_by(func.count().desc()))).all()
    top = (await db.execute(select(Member.id, Member.email, func.count().label("n")).join(AiUsage, AiUsage.member_id == Member.id)
                     .where(AiUsage.created_at >= now - timedelta(days=7)).group_by(Member.id, Member.email).order_by(func.count().desc()).limit(10))).all()
    rows = (await db.scalars(select(AiUsage.created_at).where(AiUsage.created_at >= now - timedelta(days=14)))).all()
    return {"per_tool": per_tool, "top": top, "series": _days(rows, "x", 14, now),
            "total_30d": sum(r[1] for r in per_tool), "tokens_30d": sum(r[2] + r[3] for r in per_tool)}


async def referrals_page(db: AsyncSession, status: str = "", page: int = 1) -> tuple[int, list[dict]]:
    a, b = Member.__table__.alias("referrer"), Member.__table__.alias("referred")
    stmt = select(Referral, a.c.email.label("referrer_email"), b.c.email.label("referred_email")).join(a, a.c.id == Referral.referrer_id).join(b, b.c.id == Referral.referred_id)
    if status in ("signed_up", "rewarded", "withdrawn", "capped", "skipped"):
        stmt = stmt.where(Referral.status == status)
    total = (await db.scalar(select(func.count()).select_from(stmt.subquery()))) or 0
    rows = (await db.execute(stmt.order_by(Referral.id.desc()).offset((max(1, page) - 1) * PER_PAGE).limit(PER_PAGE))).all()
    return total, [{"r": r, "referrer": re, "referred": rd} for r, re, rd in rows]


async def audit_page(db: AsyncSession, action: str = "", admin: str = "", page: int = 1) -> tuple[int, list]:
    stmt = select(AdminAudit)
    if action.strip():
        stmt = stmt.where(AdminAudit.action.like(action.strip()[:40].replace("%", "") + "%"))
    if admin.strip():
        stmt = stmt.where(func.lower(AdminAudit.admin_email) == admin.strip().lower())
    total = (await db.scalar(select(func.count()).select_from(stmt.subquery()))) or 0
    return total, (await db.scalars(stmt.order_by(AdminAudit.id.desc()).offset((max(1, page) - 1) * PER_PAGE).limit(PER_PAGE))).all()


async def admins_list(db: AsyncSession) -> list:
    return (await db.scalars(select(User).order_by(User.id))).all()


async def system_info(db: AsyncSession) -> dict:
    from app import schema
    tables = {"members": Member, "companies": Company, "jobs": Job, "subscriptions": Subscription, "checkouts": BillingCheckout, "ai calls": AiUsage,
              "audit entries": AdminAudit}
    try:
        revision, head = schema.current_revision(), schema.head_revision()
    except Exception:
        revision = head = None
    return {"counts": {k: (await db.scalar(select(func.count()).select_from(m))) or 0 for k, m in tables.items()}, "revision": revision, "head": head,
            "dialect": db.bind.dialect.name, "site_url": site.PUBLIC_URL, "billing_on": site.BILLING_ENABLED, "currency": site.BILLING_CURRENCY,
            "plans": {k: v["price_usd"] for k, v in site.CONSUMER_PLANS.items()}, "refund_days": site.REFUND_DAYS, "support": site.SUPPORT_EMAIL}


async def ai_settings_view(db: AsyncSession) -> dict:
    """What the AI settings page shows: the value in effect for each setting, where it comes from, and whether AI is working."""
    stored = await runtime_settings.values()
    s, why = await ai.resolve()
    key = stored.get("ai_api_key")
    env_key = bool(site.AI_API_KEY or site.ANTHROPIC_API_KEY or site.OPENAI_API_KEY)
    plans = [{"plan": p, "price": info["price_usd"], "default": info["ai_requests_daily"], "stored": stored.get(f"ai_requests_{p}")} for p, info in site.CONSUMER_PLANS.items()]
    env = {"provider": site.AI_PROVIDER, "base_url": site.AI_BASE_URL, "model": site.AI_MODEL, "token_param": site.AI_TOKEN_PARAM, "timeout": site.AI_TIMEOUT,
           "max_output_tokens": site.AI_MAX_OUTPUT_TOKENS, "max_concurrency": site.AI_MAX_CONCURRENCY, "json_mode": site.AI_JSON_MODE, "free_trials": site.AI_FREE_REQUESTS}
    info = await runtime_settings.stored_info(db)
    last = max(info.values(), key=lambda i: i["updated_at"]) if info else None
    return {"on": bool(s), "why": why, "effective": s, "stored": stored, "env": env, "plans": plans, "enabled": stored.get("ai_enabled") is not False,
            "key": {"stored": key is not None, "hint": ("..." + key[-4:]) if key else "", "env": env_key}, "unreadable": (await runtime_settings.unreadable()),
            "last_change": last, "ttl": int(runtime_settings.TTL)}
