import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import uuid
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import bcrypt
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.exception_handlers import http_exception_handler as default_http_handler
from fastapi.exception_handlers import request_validation_exception_handler as default_validation_handler
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from admin import config as admin_config
from admin import actions as admin_actions
from admin.main import app as platform_admin_app

from . import coach as coach_mod, lens as lens_mod, resume_upload, ai, alerts, billing, cache as cache_mod, companies, config, currency, dedupe, follows, interview, letters, lookup as lookup_mod, mailer, match as match_mod, members, onboarding as onboarding_mod, payments, referrals, resume as resume_mod, runtime_settings, salaries, schema, security, tailor, tracker, prepare as prepare_mod, reminders, reports, textfmt
from . import search as search_mod
from .db import SessionLocal, current_engine, engine, get_db, use_private_engine
from .models import (AiUsage, BillingCheckout, ClickEvent, Company, Job, JobRequirement, Member, MemberProfile, Org, Resume, SavedJob, Subscriber, User)
from . import classify as classify_mod
from . import direct
from . import filters as filters_mod
from . import scrapers as scrapers_mod
from .filters import NO_FILTERS, Filters
from .scrapers import ATS, scrape_all, scrape_company
from .seed import seed_registry

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates",
                            context_processors=[lambda r: {"memail": r.session.get("memail"), "ai_on": ai.enabled_cached(), "invited": bool(r.session.get("ref")),
                                                                                    "cur": currency.for_request(r), "approx": currency.approx, "currencies": currency.CURRENCIES}])
templates.env.filters["ago"] = lambda d: ago(d)
templates.env.filters["rich"] = lambda text, limit=None: textfmt.render(text, limit)   # a posting as headings, bullets and paragraphs
templates.env.globals["progress"] = lambda s: interview.progress(s)
templates.env.globals["CSS_V"] = int((BASE / "static" / "app.css").stat().st_mtime)    # a changed stylesheet gets a new address, so browsers never keep an old copy
templates.env.globals.update(PLATFORM=config.PLATFORM_NAME, PLANS=config.PLANS, CONSUMER_PLANS=config.CONSUMER_PLANS,
                             REFUND_DAYS=config.REFUND_DAYS, SUPPORT_EMAIL=config.SUPPORT_EMAIL)
RESERVED = {"www", "app", "admin", "api", "static", "login", "signup", "mail"}
PAGE = 20


async def _bootstrap_admin_from_environment() -> None:
    email = os.getenv("ADMIN_BOOTSTRAP_EMAIL", "")
    password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "")
    if not email and not password:
        return
    if not email or not password:
        raise RuntimeError("Set both ADMIN_BOOTSTRAP_EMAIL and ADMIN_BOOTSTRAP_PASSWORD, or remove both.")

    async with SessionLocal() as db:
        existing = await db.scalar(select(User).where(User.role.in_(admin_actions.ROLES)).limit(1))
        if existing:
            logging.getLogger("careersniper.admin").warning(
                "Admin bootstrap was skipped because an active admin already exists; remove "
                "ADMIN_BOOTSTRAP_EMAIL and ADMIN_BOOTSTRAP_PASSWORD from the service environment."
            )
            return
        try:
            await admin_actions.create_admin(
                db, None, email, password, "owner", ip="render-bootstrap", by="environment-bootstrap"
            )
        except admin_actions.AdminError as exc:
            raise RuntimeError(f"Could not bootstrap the initial admin: {exc}") from exc
    logging.getLogger("careersniper.admin").warning(
        "Initial owner created from bootstrap environment variables. Remove "
        "ADMIN_BOOTSTRAP_EMAIL and ADMIN_BOOTSTRAP_PASSWORD from the service environment."
    )


SCHEDULER_LOCK_ID = 727274   # arbitrary app-wide key for the Postgres advisory lock


async def scheduler_tick() -> bool:
    """One cycle: refresh stale boards, then send due alert digests. A session-level Postgres advisory lock makes sure
    only one app instance runs a cycle at a time, so a second instance can never double-send digests or hammer employers.
    Returns False when another instance holds the lock."""
    async with current_engine().connect() as lock:
        if not (await lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SCHEDULER_LOCK_ID})).scalar():
            return False
        try:
            async with SessionLocal() as db:
                scraped = await scrape_all(db, only_stale_hours=config.SCRAPE_INTERVAL_HOURS)
                # Legacy employer maintenance hooks are no-ops (stubs in direct/payments).
                direct.expire_due(db)
                direct.purge_unpaid(db)
                payments.expire_credits(db)
                payments.warn_expiring_credits(db)
                payments.notify_plan_renewals(db)
                direct.notify_expiring(db)
                sent = await alerts.run_digests(db)
                follow_sent = await follows.run_digests(db)
                reminded = await reminders.run(db)
            print(f"[scheduler] refreshed {scraped}; digests {sent}; follow digests {follow_sent}; expiry reminders {reminded}", flush=True)
            return True
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SCHEDULER_LOCK_ID})


async def _scheduler_loop():
    """Tick shortly after start, then hourly."""
    delay = 60
    while True:
        await asyncio.sleep(delay)
        delay = 3600
        try:
            await scheduler_tick()
        except Exception:
            logging.getLogger("careersniper").exception("scheduler tick failed")


def _scheduler():
    """The scheduler runs in its own thread with its own event loop and database engine, so a long scrape never competes with web requests."""
    use_private_engine()
    asyncio.run(_scheduler_loop())


@asynccontextmanager
async def lifespan(app):
    fatal, warn = await config.production_problems()
    for w in warn:
        logging.getLogger("careersniper").warning("config: %s", w)
    if fatal:
        raise RuntimeError("Refusing to start in production with an unsafe configuration:\n  - " + "\n  - ".join(fatal)
                           + "\nRun `python -m app.cli doctor` for details.")
    admin_problems = admin_config.problems()
    if admin_problems and admin_config.IS_PROD:
        raise RuntimeError("Refusing to start the admin app:\n  - " + "\n  - ".join(admin_problems))
    for problem in admin_problems:
        logging.getLogger("careersniper.admin").warning("config: %s", problem)
    if config.SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.init(dsn=config.SENTRY_DSN, send_default_pii=False, traces_sample_rate=0.0)
        except ImportError:
            logging.getLogger("careersniper").warning("SENTRY_DSN is set but sentry-sdk is not installed")
    if config.SCHEMA_MODE == "migrations" and not schema.is_current():
        raise RuntimeError(f"The database schema ({schema.current_revision() or 'empty'}) is behind this version of the app "
                           f"({schema.head_revision()}). Run `alembic upgrade head` before starting the app.")
    await _bootstrap_admin_from_environment()
    await search_mod.ensure_fts()
    await dedupe.backfill()
    async with SessionLocal() as db:
        await filters_mod.backfill(db)
        await seed_registry(db)
    if config.ENABLE_SCHEDULER:
        threading.Thread(target=_scheduler, daemon=True).start()
    try:
        yield
    finally:
        await cache_mod.close()


app = FastAPI(title=config.PLATFORM_NAME, lifespan=lifespan,
              docs_url=None if config.IS_PROD else "/docs", redoc_url=None if config.IS_PROD else "/redoc",
              openapi_url=None if config.IS_PROD else "/openapi.json")   # no public API schema in production
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, same_site="lax", https_only=config.IS_PROD, max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.mount("/platform-admin", platform_admin_app, name="platform-admin")
_RID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def decorate(response, request: Request):
    """Security headers + request id on a response; in production also force the Secure flag on every cookie."""
    for k, v in security.security_headers(https=config.IS_PROD).items():
        response.headers.setdefault(k, v)
    response.headers["X-Request-ID"] = getattr(request.state, "request_id", "-")
    if config.IS_PROD:
        cookies = response.headers.getlist("set-cookie")
        if cookies and any("secure" not in c.lower() for c in cookies):
            del response.headers["set-cookie"]
            for c in cookies:
                response.headers.append("set-cookie", c if "secure" in c.lower() else c + "; Secure")
    return response


@app.middleware("http")
async def hardening(request: Request, call_next):
    given = request.headers.get("x-request-id", "")
    request.state.request_id = given if _RID.match(given) else uuid.uuid4().hex[:16]
    size = request.headers.get("content-length", "")
    cap = UPLOAD_BODY_BYTES if request.url.path == "/resume/upload" else MAX_BODY_BYTES
    if size.isdigit() and int(size) > cap:   # nothing in this app legitimately posts more; also cap it at the proxy
        return decorate(JSONResponse({"detail": "Request too large"}, status_code=413), request)
    if not request.url.path.startswith("/static"):
        await runtime_settings.values()      # keeps the cache that templates read synchronously (ai.enabled_cached) fresh; a no-op while it is warm
    return decorate(await call_next(request), request)


TITLES = {400: "Bad request", 401: "Please sign in", 403: "Not allowed", 404: "Page not found", 405: "Not allowed", 413: "Too large",
          422: "Some details are missing or invalid", 429: "Too many requests"}


def wants_html(request: Request) -> bool:
    p = request.url.path
    return "text/html" in request.headers.get("accept", "") and not p.startswith(("/api/", "/webhooks/", "/internal/", "/tools/ai/"))


def error_page(request: Request, status: int, message: str = ""):
    ctx = {"status": status, "title": TITLES.get(status, "Something went wrong"), "message": message,
           "rid": getattr(request.state, "request_id", "-")}
    return templates.TemplateResponse(request, "error.html", ctx, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code in (301, 302, 303, 307, 308) or not wants_html(request):
        return decorate(await default_http_handler(request, exc), request)
    msg = exc.detail if isinstance(exc.detail, str) and exc.status_code in (400, 403, 413, 429) else ""
    resp = error_page(request, exc.status_code, msg)
    for k, v in (exc.headers or {}).items():
        resp.headers[k] = v
    return decorate(resp, request)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if not wants_html(request):
        return decorate(await default_validation_handler(request, exc), request)
    return decorate(error_page(request, 422), request)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    """Never show internals. The full traceback goes to the log (and Sentry) with the request id the visitor can quote."""
    rid = getattr(request.state, "request_id", "-")
    logging.getLogger("careersniper").error("unhandled error request_id=%s %s %s", rid, request.method, request.url.path, exc_info=exc)
    if wants_html(request):
        return decorate(error_page(request, 500), request)
    return decorate(JSONResponse({"detail": "Internal Server Error", "request_id": rid}, status_code=500), request)


MAX_BODY_BYTES = 1_000_000
UPLOAD_BODY_BYTES = 2_600_000      # the résumé upload route: a 2 MB file plus the multipart envelope
API_RATE_LIMIT = 120   # JSON API requests per organization per minute


def throttle(request: Request, name: str, limit: int, window: int, extra: str = "") -> None:
    key = f"{name}:{security.client_ip(request)}" + (f":{extra}" if extra else "")
    if not security.limiter.hit(key, limit, window):
        raise HTTPException(429, "Too many requests. Please wait a while and try again.", headers={"Retry-After": str(window)})


async def db_ping() -> None:
    async with engine.connect() as c:
        await c.execute(text("SELECT 1"))


def _pw_tag(obj) -> str:
    """Tail of the password hash. Stored in the session so changing the password ends every older session."""
    return obj.password_hash[-10:]


# ---------------------------------------------------------------- tenancy
def resolve_org(request: Request, db: AsyncSession) -> Org | None:
    """B2C single-tenant: always the platform org. Multi-tenant subdomains are retired."""
    from .models import PLATFORM_ORG
    return PLATFORM_ORG


def tenant(request: Request, db: AsyncSession = Depends(get_db)) -> Org:
    """Always the global platform (B2C)."""
    from .models import PLATFORM_ORG
    return PLATFORM_ORG


def start_admin_session(request: Request, user: User) -> None:
    request.session["uid"], request.session["ph"] = user.id, _pw_tag(user)


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    user = (await db.get(User, uid)) if uid else None
    if user and request.session.get("ph") != _pw_tag(user):   # password changed since this session started
        request.session.pop("uid", None)
        request.session.pop("ph", None)
        return None
    return user


def require_admin(user: User | None = Depends(current_user)) -> User:
    if not user:
        raise HTTPException(303, headers={"Location": "/login"})
    role = (getattr(user, "role", None) or "").lower()
    if role not in ("admin", "owner") and not getattr(user, "is_admin", False):
        raise HTTPException(403, "Admin access required")
    return user


def _csv(s: str) -> list[str]:
    return [x.strip().lower() for x in (s or "").split(",") if x.strip()]


def _badge(org: Org) -> bool:
    """B2C: never show a multi-tenant 'powered by' badge."""
    if org is None:
        return False
    limits = getattr(org, "limits", None) or {}
    if org.hide_branding and limits.get("remove_branding"):
        return False
    # Platform org always hides badge
    return False


MAX_AGE_LIMIT = 3650


def feed_query(org: Org = None):
    """Global B2C feed: all active jobs from the shared pool (no per-tenant filtering)."""
    q = select(Job).join(Company).where(Job.active.is_(True), Company.active.is_(True))
    q = q.where(or_(Job.status.is_(None), Job.status == "live"), or_(Job.expires_at.is_(None), Job.expires_at > datetime.utcnow()))
    # Soft max age from platform default
    max_age = getattr(org, "job_age_limit", None) or config.DEFAULT_MAX_JOB_AGE_DAYS
    if max_age:
        q = q.where(func.coalesce(Job.posted_at, Job.first_seen) >= datetime.utcnow() - timedelta(days=max_age))
    return q


def _feed_key(org: Org) -> tuple:
    """Everything that shapes a board's feed, so cached country options are never shown to a board they do not fit."""
    return (org.id, org.only_own_companies, org.include_keywords, org.exclude_keywords, org.remote_only, org.job_age_limit)


def search(org: Org, qs: str, loc: str, remote: bool, days: int, fts: bool | None = None, flt: Filters = NO_FILTERS, lens=None,
           exclude: set | None = None):
    """Filter query for a board. Text matching uses Postgres full-text when available (fts=None: auto)."""
    q = feed_query(org)
    qs = qs.strip()
    if qs:
        if fts is None:
            fts = search_mod.is_pg()
        if fts:
            q = q.where(search_mod.fts_clause(qs)[0])
        else:
            for cond in search_mod.like_conditions(qs):
                q = q.where(cond)
    if loc:
        q = q.where(func.lower(Job.location).contains(loc.lower()))
    if remote:
        q = q.where(Job.remote.is_(True))
    if days:
        # the date the card shows: when the employer posted it, else when we first saw it (first_seen alone made every freshly
        # scraped job look new)
        q = q.where(func.coalesce(Job.posted_at, Job.first_seen) >= datetime.utcnow() - timedelta(days=days))
    q = flt.apply(q)
    if exclude:
        q = q.where(Job.id.notin_(exclude))      # jobs the member disliked: removed in SQL so pages and totals stay exact
    return lens.apply(q) if lens is not None else q


SORTS = {"new": "Newest", "pay": "Highest pay"}


async def search_page(db: AsyncSession, org: Org, qs: str, loc: str, remote: bool, days: int, offset: int, limit: int,
                flt: Filters = NO_FILTERS, sort: str = "new", lens=None, exclude: set | None = None):
    """(total, rows) of distinct listings (identical postings collapsed). Postgres: relevance-ranked full-text,
    falling back to substring matching when full-text finds nothing."""
    qs = qs.strip()
    for fts in ([True, False] if qs and search_mod.is_pg() else [False]):
        base = search(org, qs, loc, remote, days, fts=fts, flt=flt, lens=lens, exclude=exclude)
        rank = search_mod.fts_clause(qs)[1] if (qs and fts) else None
        total, rows = await dedupe.page(db, base, rank, offset, limit, sort=sort)
        if total or not fts:
            break
    return total, rows


# ---------------------------------------------------------------- public B2C board
PREVIEW_DESC_CHARS = 1500   # enough to judge fit: the apply link and the filters are what is behind the paywall


async def _member_has_access(member: Member | None, db: AsyncSession) -> bool:
    """True if the member has an active paid period. With billing off, only dev (or ALLOW_BILLING_OFF) grants access."""
    if not member:
        return False
    if config.free_access():
        return True
    if not config.BILLING_ENABLED:
        return False   # production lost its Paystack key: fail closed
    if not member.verified:
        return False   # a plan belongs to whoever owns the mailbox: signing up with someone else's address must not borrow it
    return await billing.has_active_access(db, email=member.email)


_stats_cache: dict = {"at": 0.0, "value": None}
STATS_TTL = 300   # seconds: the homepage is public, so never run the counting queries on every hit


def ago(then: datetime | None, now: datetime | None = None) -> str:
    """'3 hours ago' for the homepage counter."""
    if not then:
        return ""
    secs = max(0, int(((now or datetime.utcnow()) - then).total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size * (1.5 if unit == "minute" else 1):
            n = secs // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


async def site_stats(db: AsyncSession, force: bool = False) -> dict | None:
    """Live jobs, employers they come from, and when the pool was last refreshed. None while the pool is empty."""
    if not force and _stats_cache["value"] is not None and time.time() - _stats_cache["at"] < STATS_TTL:
        return _stats_cache["value"] or None
    if not force:
        shared = await cache_mod.get_json("public:site-stats:v1")
        if (isinstance(shared, dict) and isinstance(shared.get("jobs"), int)
                and isinstance(shared.get("companies"), int) and isinstance(shared.get("updated"), str)):
            _stats_cache.update(at=time.time(), value=shared)
            return shared
    live = feed_query().subquery()
    jobs = (await db.scalar(select(func.count()).select_from(live))) or 0
    companies = (await db.scalar(select(func.count(func.distinct(live.c.company_id))))) or 0
    last = await db.scalar(select(func.max(Company.last_scraped)).where(Company.active.is_(True)))
    value = {"jobs": jobs, "companies": companies, "updated": ago(last)} if jobs else {}
    _stats_cache.update(at=time.time(), value=value)
    if value:
        await cache_mod.set_json("public:site-stats:v1", value, STATS_TTL)
    return value or None


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: AsyncSession = Depends(get_db)):
    """Marketing homepage (CareerHound-style)."""
    return templates.TemplateResponse(request, "marketing.html", {"stats": (await site_stats(db))})


@app.get("/refunds", response_class=HTMLResponse)
def refund_policy(request: Request):
    return templates.TemplateResponse(request, "refunds.html", {})


@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request, db: AsyncSession = Depends(get_db), cur: str = ""):
    picked = currency.valid(cur)
    ctx = {"stats": (await site_stats(db))}                            # the plans live on this page, not on the landing page
    if picked:
        request.state.cur = picked                # used by the page being built now; the cookie covers the next requests
    response = templates.TemplateResponse(request, "pricing.html", ctx)
    if picked:
        response.set_cookie(currency.COOKIE, picked, max_age=60 * 60 * 24 * 365, samesite="lax", secure=config.IS_PROD)
    return response


PREVIEW_QUERIES_PER_DAY = 25


def _preview_query_cap(request: Request, *parts) -> None:
    """A visitor on the free preview may run a limited number of *different* searches a day. The same search again is free, so
    paging back and forth never counts. Counted per browser session and, for each new search, per address (clearing cookies
    does not reset the address)."""
    sig = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]
    day = datetime.utcnow().strftime("%Y%m%d")
    book = request.session.get("pq") or {}
    if book.get("d") != day:
        book = {"d": day, "s": []}
    if sig in book["s"] or not any(parts):
        return
    if len(book["s"]) >= PREVIEW_QUERIES_PER_DAY or not security.limiter.hit(f"pvq:{security.client_ip(request)}", PREVIEW_QUERIES_PER_DAY * 4, 86400):
        raise HTTPException(429, "You have run a lot of searches on the free preview today. Create a free account or pick a plan to keep searching.")
    book["s"] = book["s"] + [sig]
    request.session["pq"] = book


@app.get("/jobs", response_class=HTMLResponse)
async def job_search(request: Request, db: AsyncSession = Depends(get_db), q: str = "", loc: str = "",
         remote: bool = False, days: int = 0, page: int = Query(1, ge=1), country: str = "", type: str = "",
         level: str = "", paid: str = "", min_pay: str = "", max_pay: str = "", remote_from: str = "",
         visa: str = "", sort: str = "new", unlocked: int = 0, for_: str = Query("", alias="for")):
    """Job search with free preview for non-subscribers. A member with a résumé sees jobs that fit it unless they ask for everything
    (for=all) or are searching for something specific (q)."""
    sort = sort if sort in SORTS else "new"
    org = resolve_org(request, db)
    member = await current_member(request, db)
    has_access = await _member_has_access(member, db)
    flt = Filters.make(country, type, level, paid, min_pay, max_pay, remote_from, visa)
    # Preview: limit page and results for free users
    if not has_access:
        # The preview is capped per query, so cap queries too: otherwise varied searches walk the whole database.
        throttle(request, "preview", 30, 60)
        _preview_query_cap(request, q, loc, remote, days, country, type, level, paid, min_pay, max_pay, remote_from, visa)
        page = 1
        limit = config.PREVIEW_JOB_LIMIT
    else:
        limit = PAGE
    lens_ctx = await _lens_context(db, member, q, for_)
    reactions = await members.reaction_map(db, member)
    disliked = {jid for jid, st in reactions.items() if st == "disliked"} if reactions else set()   # hidden from the feed, in SQL
    total, rows = await search_page(db, org, q, loc, remote, days, (page - 1) * limit, limit, flt=flt, sort=sort, lens=lens_ctx["lens"],
                                    exclude=disliked or None)
    if not has_access and total > config.PREVIEW_JOB_LIMIT:
        rows = rows[: config.PREVIEW_JOB_LIMIT]
    saved = {jid for jid, st in reactions.items() if st in ("liked", "saved", "applied")}
    sib = await dedupe.locations_for(db, feed_query(org), rows)
    return templates.TemplateResponse(request, "board.html", dict(
        why=({j.id: lens_ctx["lens"].why(j.title) for j in rows} if lens_ctx["lens"] is not None else {}),
        lens_offer=lens_ctx["offer"], lens_on=lens_ctx["lens"] is not None, lens_paused=lens_ctx["paused"], lens_hint=lens_ctx["hint"], for_all=lens_ctx["for_all"],
        unlocked=bool(unlocked and has_access), sort=sort, sorts=SORTS, sib=sib, saved=saved, reactions=reactions, org=org, jobs=rows, total=total, q=q, loc=loc,
        remote=remote, days=days, page=page,
        flt=flt, countries=(await filters_mod.country_options(db, feed_query(org), _feed_key(org))), job_types=filters_mod.TYPES,
        levels=filters_mod.LEVELS, country_names=classify_mod.COUNTRY_NAMES, pay_steps=filters_mod.PAY_STEPS,
        remote_regions=(await filters_mod.remote_region_options(db, feed_query(org), _feed_key(org))), visa_choices=filters_mod.VISA_CHOICES,
        pages=max(1, -(-total // PAGE)) if has_access else 1,
        now=datetime.utcnow(), show_badge=False,
        has_access=has_access, preview_limit=config.PREVIEW_JOB_LIMIT,
        is_preview=not has_access))


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(job_id: int, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db)):
    job = await db.scalar(feed_query(org).where(Job.id == job_id))
    if not job:
        raise HTTPException(404, "Job not found")
    saved = await members.saved_ids(db, (await current_member(request, db)))
    others = (await dedupe.locations_for(db, feed_query(org), [job])).get(job.dup_key, []) if job.dup_key else []
    others = [(i, l) for i, l in others if i != job.id and l != job.location]
    tool_keys = set()  # career tools not registered in B2C seeker surface
    me = await current_member(request, db)
    has_access = await _member_has_access(me, db)
    req_row = await db.get(JobRequirement, job.id)          # the shared analysis of this posting, when one has been made (never made on a page view)
    reqs = req_row.data if req_row and isinstance(req_row.data, dict) and (req_row.data.get("must_have") or req_row.data.get("responsibilities")) else None
    return templates.TemplateResponse(request, "job.html", dict(**(await _coach_context(db, me, job)), reqs=reqs, org=org, job=job, saved=saved, others=others, tool_keys=tool_keys, show_badge=_badge(org), now=datetime.utcnow(), has_access=has_access, preview_chars=PREVIEW_DESC_CHARS,
                                                              signed_in=me is not None, tracked=(await tracker.tracked_status(db, me, job.id)), tracker_labels=tracker.LABELS, company_path=companies.path(job.company), salary_path=(await salaries.path_for_title(db, job.title))))


@app.get("/go/{job_id}")
async def apply_redirect(job_id: int, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db)):
    """Send the seeker to the employer's own apply URL; count at most one click per IP/job/10min. Subscribers only."""
    me = await current_member(request, db)
    if not (await _member_has_access(me, db)):
        # straight to a plan with the job remembered, so paying returns them to it (not to a generic pricing page)
        return RedirectResponse(f"/checkout?plan=monthly&next={quote(f'/jobs/{job_id}', safe='')}", status_code=303)
    job = await db.scalar(feed_query(org).where(Job.id == job_id))
    if not job or not (job.url or "").strip():
        raise HTTPException(404, "Job not found")
    if me:       # clicking Apply puts the job in their tracker, so the tracker fills itself in
        try:
            app_row, created = await tracker.track_job(db, me, job)
            if created or app_row.status == "saved":
                app_row.status = "applied"
                tracker._stamp(app_row, "applied", datetime.utcnow())
                await db.commit()
        except tracker.TrackerError:
            await db.rollback()
    url = job.url.strip()
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("mailto:")):
        raise HTTPException(404, "Job link unavailable")
    if security.limiter.hit(f"go:{security.client_ip(request)}:{job.id}", 1, 600):
        try:
            db.add(ClickEvent(job_id=job.id))
            await db.commit()
        except Exception:
            await db.rollback()
    return RedirectResponse(url, status_code=302)


# ---------------------------------------------------------------- member accounts & saved jobs (tenant host)
async def current_member(request: Request, db: AsyncSession = Depends(get_db)) -> Member | None:
    mid = request.session.get("mid")
    m = (await db.get(Member, mid)) if mid else None
    if m and request.session.get("mph") != _pw_tag(m):   # password reset since this session started
        m = None
    if mid and not m:
        for k in ("mid", "memail", "mph", "mv"):
            request.session.pop(k, None)
    if m:
        paid = request.session.get("paid_email")
        if paid and paid == m.email and not m.verified:    # they just paid with this address in this browser
            m.verified = True
            await db.commit()
        request.session["mv"] = bool(m.verified)           # read by the header banner, so no extra query per page
    return m


def _remember_member(response):
    """Mark this browser as belonging to someone with an account, so the header offers Sign in instead of Sign up (no personal data)."""
    response.set_cookie("cs_member", "1", max_age=60 * 60 * 24 * 365 * 2, samesite="lax", secure=config.IS_PROD, httponly=True)
    return response


def start_member_session(request: Request, m: Member) -> None:
    request.session["mid"], request.session["memail"], request.session["mph"] = m.id, m.email, _pw_tag(m)
    request.session["mv"] = bool(m.verified)


def _member_page(request, org, title, body, status=200):
    return _alert_page(request, org, title, body, status)


def _member_form(request, org, template, status=200, **ctx):
    context = {"org": org, "show_badge": _badge(org), "error": "", **ctx}   # ctx may override error
    return templates.TemplateResponse(request, template, context, status_code=status)


@app.get("/member/signup", response_class=HTMLResponse)
def member_signup_form(request: Request, org: Org = Depends(tenant), next: str = "/"):
    return _member_form(request, org, "member_signup.html", next=members.safe_next(next))


@app.post("/member/signup")
async def member_signup(request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
                  email: str = Form(""), password: str = Form(""), next: str = Form("/"), website: str = Form("")):
    if website:  # honeypot
        return RedirectResponse("/", status_code=303)
    throttle(request, "msignup", 20, 3600)
    try:
        m = await members.signup(db, org, email, password)
    except members.MemberError as e:
        return _member_form(request, org, "member_signup.html", e.status, error=str(e), next=members.safe_next(next))
    if request.session.get("paid_email") == m.email and not m.verified:
        m.verified = True                                # they paid with this address in this browser: that proves the mailbox
        await db.commit()
    start_member_session(request, m)
    code = request.session.pop("ref", None)             # came through a friend's invite link
    if code:
        await referrals.attach(db, m, code)
    dest = members.safe_next(next)
    if dest in ("/", ""):
        dest = "/onboarding?welcome=1"                   # questionnaire first, then pricing / résumé
    elif dest.startswith("/jobs") and "unlocked" in dest:
        dest = "/onboarding?welcome=1&next=" + quote(dest, safe="")
    return _remember_member(RedirectResponse(dest, status_code=303))


# ── First-time onboarding questionnaire ─────────────────────────────────────

@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_form(request: Request, db: AsyncSession = Depends(get_db), next: str = "/"):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/onboarding", status_code=303)
    if await onboarding_mod.is_complete(db, m):
        dest = members.safe_next(next)
        if dest in ("/", ""):
            dest = "/pricing?from=onboarding"
        return RedirectResponse(dest, status_code=303)
    profile = await onboarding_mod.get_profile(db, m)
    pitch = onboarding_mod.pitch_for_frustration(
        (profile.main_frustration if profile else "") or "other"
    )
    return templates.TemplateResponse(request, "onboarding.html", dict(
        profile=profile,
        pitch=pitch,
        next=members.safe_next(next),
        employment_status=onboarding_mod.EMPLOYMENT_STATUS,
        experience_levels=onboarding_mod.EXPERIENCE_LEVELS,
        job_types=onboarding_mod.JOB_TYPES,
        remote_prefs=onboarding_mod.REMOTE_PREFS,
        salary_ranges=onboarding_mod.SALARY_RANGES,
        frustrations=onboarding_mod.FRUSTRATIONS,
        countries=onboarding_mod.COMMON_COUNTRIES,
        error="",
        org=None,
    ))


@app.post("/onboarding")
async def onboarding_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    next: str = Form("/"),
    intent: str = Form("pricing"),
    employment_status: str = Form(""),
    desired_titles: str = Form(""),
    experience_level: str = Form(""),
    salary_range: str = Form(""),
    job_types: list[str] = Form([]),
    work_country: str = Form(""),
    remote_preference: str = Form(""),
    main_frustration: str = Form(""),
    has_resume: str = Form(""),
):
    from urllib.parse import urlencode
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/onboarding", status_code=303)
    data = {
        "employment_status": employment_status,
        "desired_titles": desired_titles,
        "experience_level": experience_level,
        "salary_range": salary_range,
        "job_types": job_types if isinstance(job_types, list) else [job_types],
        "work_country": work_country,
        "remote_preference": remote_preference,
        "main_frustration": main_frustration,
        "has_resume": has_resume,
    }
    try:
        await onboarding_mod.save_profile(db, m, data, mark_complete=True)
    except onboarding_mod.OnboardingError as e:
        profile = await onboarding_mod.get_profile(db, m)
        pitch = onboarding_mod.pitch_for_frustration(main_frustration or "other")
        return templates.TemplateResponse(request, "onboarding.html", dict(
            profile=profile,
            pitch=pitch,
            next=members.safe_next(next),
            employment_status=onboarding_mod.EMPLOYMENT_STATUS,
            experience_levels=onboarding_mod.EXPERIENCE_LEVELS,
            job_types=onboarding_mod.JOB_TYPES,
            remote_prefs=onboarding_mod.REMOTE_PREFS,
            salary_ranges=onboarding_mod.SALARY_RANGES,
            frustrations=onboarding_mod.FRUSTRATIONS,
            countries=onboarding_mod.COMMON_COUNTRIES,
            error=str(e),
            org=None,
        ), status_code=e.status)
    safe = members.safe_next(next)
    after_pay = safe if safe not in ("/", "") else (
        "/resume?welcome=1" if has_resume in ("1", "true", "yes", "on") else "/jobs?onboarded=1"
    )
    if intent == "preview":
        dest = after_pay if after_pay.startswith("/jobs") else "/jobs?onboarded=1"
        return RedirectResponse(dest, status_code=303)
    dest = "/pricing?" + urlencode({"from": "onboarding", "next": after_pay})
    return RedirectResponse(dest, status_code=303)


@app.get("/member/login", response_class=HTMLResponse)
def member_login_form(request: Request, org: Org = Depends(tenant), next: str = "/"):
    return _member_form(request, org, "member_login.html", next=members.safe_next(next))


@app.post("/member/login")
async def member_login(request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
                 email: str = Form(""), password: str = Form(""), next: str = Form("/")):
    key = f"mlogin:{email.strip().lower()}:{request.client.host if request.client else ''}"
    if members.throttled(key):
        return _member_form(request, org, "member_login.html", 429, next=members.safe_next(next),
                            error="Too many attempts. Please wait a few minutes and try again.")
    try:
        m = await members.login(db, org, email, password)
    except members.MemberError as e:
        members.record_failure(key)
        return _member_form(request, org, "member_login.html", e.status, error=str(e), next=members.safe_next(next))
    members.clear_failures(key)
    start_member_session(request, m)
    return _remember_member(RedirectResponse(members.safe_next(next), status_code=303))


@app.post("/member/logout")
def member_logout(request: Request):
    for k in ("mid", "memail", "mph", "mv", "paid_email"):
        request.session.pop(k, None)
    return RedirectResponse("/", status_code=303)


@app.get("/member/verify/{token}", response_class=HTMLResponse)
async def member_verify(token: str, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db)):
    if not (await members.verify(db, org, token)):
        return _member_page(request, org, "Link not valid", "This confirmation link is invalid or has expired. Log in and request a new one from your account page.", 404)
    return _member_page(request, org, "Email confirmed", "Thanks! Your email is confirmed.")


@app.get("/member/forgot", response_class=HTMLResponse)
def member_forgot_form(request: Request, org: Org = Depends(tenant)):
    return _member_form(request, org, "member_forgot.html")


@app.post("/member/forgot", response_class=HTMLResponse)
async def member_forgot(request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db), email: str = Form("")):
    throttle(request, "mforgot", 10, 3600)
    key = f"reset:{email.strip().lower()}"
    if not members.throttled(key):
        members.record_failure(key)   # also caps reset emails per address
        await members.send_reset(db, org, email)
    return _member_page(request, org, "Check your email", "If an account exists for that address, we sent a link to reset the password.")


@app.get("/member/reset/{token}", response_class=HTMLResponse)
async def member_reset_form(token: str, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db)):
    if not (await members.reset_token_valid(db, org, token)):
        return _member_page(request, org, "Link not valid", "This reset link is invalid or has expired. Request a new one.", 404)
    return _member_form(request, org, "member_reset.html", token=token)


@app.post("/member/reset/{token}")
async def member_reset(token: str, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db), password: str = Form("")):
    try:
        m = await members.reset_password(db, org, token, password)
    except members.MemberError as e:
        return _member_form(request, org, "member_reset.html", e.status, error=str(e), token=token)
    start_member_session(request, m)
    return RedirectResponse("/account", status_code=303)


@app.post("/saved/{job_id}")
async def saved_toggle(job_id: int, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
                 m: Member | None = Depends(current_member), next: str = Form("/")):
    """Heart / like toggle (legacy URL kept for existing forms)."""
    nxt = members.safe_next(next)
    if not m:
        return RedirectResponse(f"/member/login?next={nxt}", status_code=303)
    visible = await db.scalar(feed_query(org).where(Job.id == job_id))
    already = await db.scalar(select(SavedJob.id).where(SavedJob.member_id == m.id, SavedJob.job_id == job_id))
    if not visible and not already:
        raise HTTPException(404)
    await members.set_reaction(db, m, job_id, "liked")
    return RedirectResponse(nxt, status_code=303)


@app.post("/jobs/{job_id}/react")
async def job_react(job_id: int, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
              m: Member | None = Depends(current_member),
              reaction: str = Form("liked"), next: str = Form("/jobs")):
    """Like or dislike a job. Same reaction again clears it."""
    nxt = members.safe_next(next)
    if not m:
        return RedirectResponse(f"/member/login?next={nxt}", status_code=303)
    if reaction not in members.VALID_REACTIONS:
        raise HTTPException(400, "Invalid reaction")
    visible = await db.scalar(feed_query(org).where(Job.id == job_id))
    already = await db.scalar(select(SavedJob.id).where(SavedJob.member_id == m.id, SavedJob.job_id == job_id))
    if not visible and not already:
        raise HTTPException(404)
    await members.set_reaction(db, m, job_id, reaction)
    return RedirectResponse(nxt, status_code=303)


@app.post("/saved/{job_id}/status")
async def saved_status(job_id: int, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
                 m: Member | None = Depends(current_member), status: str = Form("liked")):
    if not m:
        return RedirectResponse("/member/login?next=/saved", status_code=303)
    await members.set_status(db, m, job_id, status)
    return RedirectResponse("/saved", status_code=303)


@app.get("/saved", response_class=HTMLResponse)
async def saved_list(request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
               m: Member | None = Depends(current_member), tab: str = "liked"):
    if not m:
        return RedirectResponse("/member/login?next=/saved", status_code=303)
    tab = tab if tab in ("liked", "disliked", "all") else "liked"
    q = select(SavedJob, Job).join(Job, Job.id == SavedJob.job_id).where(SavedJob.member_id == m.id)
    if tab == "liked":
        q = q.where(SavedJob.status.in_(("liked", "saved", "applied")))
    elif tab == "disliked":
        q = q.where(SavedJob.status == "disliked")
    rows = (await db.execute(q.order_by(SavedJob.created_at.desc()))).all()
    ids = [j.id for _, j in rows]
    visible = set((await db.scalars(feed_query(org).where(Job.id.in_(ids)).with_only_columns(Job.id)))) if ids else set()
    counts = {
        "liked": (await db.scalar(select(func.count()).select_from(SavedJob).where(
            SavedJob.member_id == m.id, SavedJob.status.in_(("liked", "saved", "applied"))))) or 0,
        "disliked": (await db.scalar(select(func.count()).select_from(SavedJob).where(
            SavedJob.member_id == m.id, SavedJob.status == "disliked"))) or 0,
    }
    return templates.TemplateResponse(request, "saved.html", dict(
        org=org, member=m, rows=rows, visible=visible, tab=tab, counts=counts,
        tool_keys=set(), show_badge=False))


# ---------------------------------------------------------------- application tracker (any signed-in member)
async def _tracker_page(request: Request, db: AsyncSession, m: Member, status: str = "all", error: str = "", code: int = 200):
    status = status if status in tracker.STATUSES else "all"
    return templates.TemplateResponse(request, "tracker.html", dict(
        org=None, status=status, labels=tracker.LABELS, error=error, **(await tracker.board(db, m, status))), status_code=code)


async def _tracker_member(request: Request, db: AsyncSession) -> Member | None:
    return await current_member(request, db)


@app.get("/tracker", response_class=HTMLResponse)
async def tracker_page(request: Request, db: AsyncSession = Depends(get_db), status: str = "all"):
    m = await _tracker_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/tracker", status_code=303)
    return await _tracker_page(request, db, m, status)


@app.post("/tracker/add")
async def tracker_add(request: Request, db: AsyncSession = Depends(get_db), company: str = Form(""), title: str = Form(""),
                url: str = Form(""), status: str = Form("applied"), notes: str = Form("")):
    m = await _tracker_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/tracker", status_code=303)
    throttle(request, "tracker", 60, 3600)
    try:
        await tracker.add_manual(db, m, company, title, url, status, notes)
    except tracker.TrackerError as e:
        return await _tracker_page(request, db, m, error=str(e), code=e.status)
    return RedirectResponse("/tracker", status_code=303)


@app.post("/jobs/{job_id}/track")
async def tracker_track_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db), next: str = Form("")):
    m = await _tracker_member(request, db)
    nxt = members.safe_next(next) if next else f"/jobs/{job_id}"
    if not m:
        return RedirectResponse(f"/member/login?next={nxt}", status_code=303)
    job = await db.scalar(feed_query().where(Job.id == job_id))
    if not job:
        raise HTTPException(404, "Job not found")
    throttle(request, "tracker", 60, 3600)
    try:
        await tracker.track_job(db, m, job)
    except tracker.TrackerError as e:
        return await _tracker_page(request, db, m, error=str(e), code=e.status)
    return RedirectResponse(nxt, status_code=303)


async def _tracker_action(request: Request, db: AsyncSession, app_id: int, fn, back: str = "/tracker"):
    m = await _tracker_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/tracker", status_code=303)
    try:
        await fn(db, m, app_id)
    except tracker.TrackerError as e:
        if e.status == 404:
            raise HTTPException(404, "Not found")
        return await _tracker_page(request, db, m, error=str(e), code=e.status)
    return RedirectResponse(back, status_code=303)


@app.post("/tracker/{app_id}/status")
async def tracker_status(app_id: int, request: Request, db: AsyncSession = Depends(get_db), status: str = Form(""), status_filter: str = Form("all")):
    back = "/tracker" + (f"?status={status_filter}" if status_filter in tracker.STATUSES else "")
    return await _tracker_action(request, db, app_id, lambda d, m, i: tracker.set_status(d, m, i, status), back)


@app.post("/tracker/{app_id}/notes")
async def tracker_notes(app_id: int, request: Request, db: AsyncSession = Depends(get_db), notes: str = Form("")):
    return await _tracker_action(request, db, app_id, lambda d, m, i: tracker.set_notes(d, m, i, notes))


@app.post("/tracker/{app_id}/delete")
async def tracker_delete(app_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    return await _tracker_action(request, db, app_id, tracker.delete)


# ---------------------------------------------------------------- résumé and job match
PROVIDER_NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "openai_compatible": "our AI provider"}


_PROFILE_NOTES = {
    "later": "We could not read your profile just now (too many requests). Try \"Read my résumé again\" later; the rest of the site works as usual.",
    "retry": "We saved your résumé but could not read its profile this time. Press \"Read my résumé again\" to try once more.",
    "unclear": "We saved your résumé but could not tell which jobs to look for. Add your job titles and skills to the text, or set the titles yourself below.",
}


async def _resume_page(request: Request, m: Member, db: AsyncSession, text: str | None = None, error: str = "", saved: bool = False, code: int = 200):
    s = await ai.settings()
    r = await resume_mod.get_main(db, m)
    profile = (r.data or {}).get("profile") if r else None
    qp = request.query_params
    return templates.TemplateResponse(request, "resume.html", dict(
        profile=profile if isinstance(profile, dict) and profile.get("titles") else None,
        profile_note=_PROFILE_NOTES.get(qp.get("note", ""), ""), uploaded=qp.get("uploaded") == "1", profile_saved=qp.get("profile") == "saved",
        has_resume=bool(r and resume_mod.text_of(r)), ai_on=bool(s), accept=resume_upload.ACCEPT, max_file_mb=resume_upload.MAX_FILE_BYTES // 1_000_000,
        levels=lens_mod.SENIORITY_LABEL,
        org=None, text=resume_mod.text_of(r) if text is None else text, error=error, saved=saved,
        min_chars=resume_mod.MIN_CHARS, max_chars=resume_mod.MAX_CHARS,
        provider=PROVIDER_NAMES.get(s.provider, "our AI provider") if s else "our AI provider"), status_code=code)


@app.get("/resume", response_class=HTMLResponse)
async def resume_page(request: Request, db: AsyncSession = Depends(get_db), saved: int = 0):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/resume", status_code=303)
    return await _resume_page(request, m, db, saved=bool(saved))


@app.post("/resume")
async def resume_save(request: Request, db: AsyncSession = Depends(get_db), text: str = Form("")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/resume", status_code=303)
    throttle(request, "resume", 30, 3600)
    try:
        r = await resume_mod.save_text(db, m, text)
    except resume_mod.ResumeError as e:
        return await _resume_page(request, m, db, text=text, error=str(e), code=e.status)
    note = await _read_profile(request, db, m, r)
    return RedirectResponse("/resume?saved=1" + (f"&note={note}" if note else ""), status_code=303)


async def _read_profile(request: Request, db: AsyncSession, m: Member, r: Resume) -> str:
    """Read the job titles, level and skills off a freshly saved résumé, free of charge. Returns a note code when it could not."""
    if not (await ai.enabled()):
        return ""
    cached = (r.data or {}).get("profile")
    if isinstance(cached, dict) and cached.get("titles"):
        return ""
    if not security.limiter.hit(f"profile:{security.client_ip(request)}", 12, 86400):
        return "later"
    try:
        await match_mod.get_profile(db, m, r, free=True)
    except ai.AIError as e:
        await db.rollback()
        return "unclear" if e.status == 422 else "retry"
    return ""


@app.post("/resume/upload")
async def resume_upload_route(request: Request, db: AsyncSession = Depends(get_db), file: UploadFile = File(None)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/resume", status_code=303)
    throttle(request, "resume", 30, 3600)
    try:
        if file is None or not file.filename:
            raise resume_mod.ResumeError("Choose a PDF, Word or text file to upload.")
        data = await file.read(resume_upload.MAX_FILE_BYTES + 1)
        text = await resume_upload.extract_text(file.filename, data)
        r = await resume_mod.save_text(db, m, text)
    except resume_mod.ResumeError as e:
        return await _resume_page(request, m, db, error=str(e), code=e.status)
    note = await _read_profile(request, db, m, r)
    return RedirectResponse("/resume?saved=1&uploaded=1" + (f"&note={note}" if note else ""), status_code=303)


@app.post("/resume/profile")
async def resume_profile_build(request: Request, db: AsyncSession = Depends(get_db)):
    """Read the profile again (free), for a résumé whose profile could not be made the first time."""
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/resume", status_code=303)
    r = await resume_mod.get_main(db, m)
    if not r or not resume_mod.text_of(r):
        return RedirectResponse("/resume", status_code=303)
    note = await _read_profile(request, db, m, r)
    return RedirectResponse("/resume?saved=1" + (f"&note={note}" if note else ""), status_code=303)


@app.post("/resume/profile/edit")
async def resume_profile_edit(request: Request, db: AsyncSession = Depends(get_db), titles: str = Form(""), seniority: str = Form("")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/resume", status_code=303)
    r = await resume_mod.get_main(db, m)
    if not r or not resume_mod.text_of(r):
        return RedirectResponse("/resume", status_code=303)
    try:
        match_mod.apply_profile_edit(r, titles, seniority)
    except ai.AIError as e:
        return await _resume_page(request, m, db, error=str(e), code=e.status)
    await db.commit()
    return RedirectResponse("/resume?profile=saved", status_code=303)


@app.post("/resume/delete")
async def resume_delete(request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/resume", status_code=303)
    await resume_mod.delete(db, m)
    return RedirectResponse("/resume", status_code=303)


async def _match_page(request: Request, db: AsyncSession, m: Member, error: str = "", code: int = 200, upgrade: bool = False, flt: Filters | None = None):
    r = await resume_mod.get_main(db, m)
    last = match_mod.last_match(r)
    rows = []
    if last:
        ids = [i["id"] for i in last["items"]]
        jobs = {j.id: j for j in (await db.scalars(feed_query().where(Job.id.in_(ids))))} if ids else {}
        tracked = set((await db.scalars(select(tracker.Application.job_id).where(tracker.Application.member_id == m.id, tracker.Application.job_id.in_(ids))))) if ids else set()
        rows = [(i, jobs[i["id"]], i["id"] in tracked) for i in last["items"] if i["id"] in jobs]
        last = {**last, "rows": rows, "at_ago": ago(datetime.fromisoformat(last["at"])) if last.get("at") else ""}
    if flt is None:
        flt = Filters.parse(last["filters"]) if last else NO_FILTERS
    return templates.TemplateResponse(request, "match.html", dict(
        org=None, ai_on=(await ai.enabled()), has_resume=bool(resume_mod.text_of(r)), match=last, error=error, upgrade=upgrade, flt=flt,
        need=match_mod.credits_needed(r) if r else 2, credits=(await ai.credits(db, m)),
        regions=(await filters_mod.remote_region_options(db, feed_query(), ("match",))), visa_choices=filters_mod.VISA_CHOICES), status_code=code)


@app.get("/match", response_class=HTMLResponse)
async def match_page(request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/match", status_code=303)
    return await _match_page(request, db, m)


@app.post("/match")
async def match_run(request: Request, db: AsyncSession = Depends(get_db), remote_from: str = Form(""), visa: str = Form(""), paid: str = Form("")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/match", status_code=303)
    flt = Filters.make(remote_from=remote_from, visa=visa, paid=paid)
    r = await resume_mod.get_main(db, m)
    if not (await ai.enabled()):
        return await _match_page(request, db, m, "Job match is not available right now.", 503, flt=flt)
    if not r or not resume_mod.text_of(r):
        return RedirectResponse("/resume", status_code=303)
    throttle(request, "match", 10, 3600)
    try:
        await match_mod.run(db, m, r, flt)
    except ai.AIError as e:
        return await _match_page(request, db, m, str(e), e.status, upgrade=e.status == 402, flt=flt)
    return RedirectResponse("/match", status_code=303)


@app.post("/jobs/{job_id}/coach")
async def job_coach(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Résumé advice for this job (uses the member's AI tokens the first time; the answer is kept until the résumé changes)."""
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/jobs/{job_id}", status_code=303)
    job = await _visible_job(db, job_id)
    r = await resume_mod.get_main(db, m)
    if not r or not resume_mod.text_of(r):
        return RedirectResponse("/resume", status_code=303)
    throttle(request, "coach", 20, 3600)
    try:
        await coach_mod.advise(db, m, r, job)
    except ai.AIError as e:
        await db.rollback()
        return RedirectResponse(f"/jobs/{job_id}?coach=" + ("upgrade" if e.status == 402 else "limit" if e.status == 429 else "confirm" if e.status == 403 else "error") + "#coach", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}#coach", status_code=303)


# ---------------------------------------------------------------- cover letters
async def _letter_form(request: Request, db: AsyncSession, m: Member, job: Job, error: str = "", code: int = 200, upgrade: bool = False,
                 need_resume: bool = False, form: dict | None = None):
    r = await resume_mod.get_main(db, m)
    form = form or {}
    return templates.TemplateResponse(request, "letter_form.html", dict(
        org=None, job=job, error=error, upgrade=upgrade, need_resume=need_resume, ai_on=(await ai.enabled()), credits=(await ai.credits(db, m)),
        existing=(await letters.latest_for_job(db, m, job.id)), tones=list(letters.TONES), lengths=letters.LENGTHS,
        name=form.get("name", (r.data or {}).get("name", "") if r else ""), tone=form.get("tone", "professional"),
        length=form.get("length", "standard"), emphasis=form.get("emphasis", "")), status_code=code)


async def _visible_job(db: AsyncSession, job_id: int) -> Job:
    job = await db.scalar(feed_query().where(Job.id == job_id))
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/jobs/{job_id}/cover-letter", response_class=HTMLResponse)
async def letter_form_page(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/jobs/{job_id}/cover-letter", status_code=303)
    return await _letter_form(request, db, m, (await _visible_job(db, job_id)))


@app.post("/jobs/{job_id}/cover-letter")
async def letter_write(job_id: int, request: Request, db: AsyncSession = Depends(get_db), name: str = Form(""), tone: str = Form("professional"),
                 length: str = Form("standard"), emphasis: str = Form("")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/jobs/{job_id}/cover-letter", status_code=303)
    job = await _visible_job(db, job_id)
    form = dict(name=name, tone=tone, length=length, emphasis=emphasis)
    if not (await ai.enabled()):
        return await _letter_form(request, db, m, job, "Cover letters are not available right now.", 503, form=form)
    throttle(request, "letter", 20, 3600)
    try:
        doc = await letters.generate(db, m, job, tone, length, emphasis, name)
    except letters.LetterError as e:
        return await _letter_form(request, db, m, job, str(e), e.status, need_resume=e.status == 409, form=form)
    except ai.AIError as e:
        return await _letter_form(request, db, m, job, str(e), e.status, upgrade=e.status == 402, form=form)
    return RedirectResponse(f"/letters/{doc.id}?fresh=1", status_code=303)


async def _letter_page(request: Request, db: AsyncSession, m: Member, doc, error: str = "", code: int = 200, content: str | None = None,
                 saved: bool = False, fresh: bool = False):
    job = (await db.get(Job, doc.job_id)) if doc.job_id else None
    tracked = bool(job and (await tracker.tracked_status(db, m, job.id)))
    return templates.TemplateResponse(request, "letter.html", dict(
        org=None, doc=doc, job=job, tracked=tracked, error=error, saved=saved, fresh=fresh, max_chars=letters.MAX_EDIT_CHARS,
        content=doc.content if content is None else content), status_code=code)


async def _letter_owned(db: AsyncSession, m: Member, doc_id: int):
    try:
        return await letters.get_owned(db, m, doc_id)
    except letters.LetterError:
        raise HTTPException(404, "Not found")


@app.get("/letters", response_class=HTMLResponse)
async def letters_list(request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/letters", status_code=303)
    return templates.TemplateResponse(request, "letters.html", dict(org=None, rows=(await letters.listing(db, m))))


@app.get("/letters/{doc_id}", response_class=HTMLResponse)
async def letter_view(doc_id: int, request: Request, db: AsyncSession = Depends(get_db), fresh: int = 0, saved: int = 0):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/letters/{doc_id}", status_code=303)
    return await _letter_page(request, db, m, (await _letter_owned(db, m, doc_id)), fresh=bool(fresh), saved=bool(saved))


@app.post("/letters/{doc_id}")
async def letter_save(doc_id: int, request: Request, db: AsyncSession = Depends(get_db), content: str = Form("")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/letters/{doc_id}", status_code=303)
    doc = await _letter_owned(db, m, doc_id)
    try:
        await letters.update(db, m, doc_id, content)
    except letters.LetterError as e:
        return await _letter_page(request, db, m, doc, error=str(e), code=e.status, content=content)
    return RedirectResponse(f"/letters/{doc_id}?saved=1", status_code=303)


@app.post("/letters/{doc_id}/delete")
async def letter_delete(doc_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/letters", status_code=303)
    await _letter_owned(db, m, doc_id)
    await letters.delete(db, m, doc_id)
    return RedirectResponse("/letters", status_code=303)


@app.get("/letters/{doc_id}/download")
async def letter_download(doc_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/letters/{doc_id}", status_code=303)
    doc = await _letter_owned(db, m, doc_id)
    return PlainTextResponse(doc.content, headers={"Content-Disposition": f'attachment; filename="{letters.filename(doc)}"'})


# ---------------------------------------------------------------- tailored résumés
async def _tailor_form(request: Request, db: AsyncSession, m: Member, job: Job, error: str = "", code: int = 200, upgrade: bool = False,
                 need_resume: bool = False, emphasis: str = ""):
    return templates.TemplateResponse(request, "tailor_form.html", dict(
        org=None, job=job, error=error, upgrade=upgrade, need_resume=need_resume, ai_on=(await ai.enabled()), credits=(await ai.credits(db, m)),
        existing=(await tailor.latest_for_job(db, m, job.id)), emphasis=emphasis), status_code=code)


@app.get("/jobs/{job_id}/tailor", response_class=HTMLResponse)
async def tailor_form_page(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/jobs/{job_id}/tailor", status_code=303)
    return await _tailor_form(request, db, m, (await _visible_job(db, job_id)))


@app.post("/jobs/{job_id}/tailor")
async def tailor_make(job_id: int, request: Request, db: AsyncSession = Depends(get_db), emphasis: str = Form("")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/jobs/{job_id}/tailor", status_code=303)
    job = await _visible_job(db, job_id)
    if not (await ai.enabled()):
        return await _tailor_form(request, db, m, job, "Résumé tailoring is not available right now.", 503, emphasis=emphasis)
    throttle(request, "tailor", 20, 3600)
    try:
        doc = await tailor.generate(db, m, job, emphasis)
    except tailor.TailorError as e:
        return await _tailor_form(request, db, m, job, str(e), e.status, need_resume=e.status == 409, emphasis=emphasis)
    except ai.AIError as e:
        return await _tailor_form(request, db, m, job, str(e), e.status, upgrade=e.status == 402, emphasis=emphasis)
    return RedirectResponse(f"/tailored/{doc.id}?fresh=1", status_code=303)


async def _tailored_owned(db: AsyncSession, m: Member, doc_id: int):
    try:
        return await tailor.get_owned(db, m, doc_id)
    except tailor.TailorError:
        raise HTTPException(404, "Not found")


async def _tailored_page(request: Request, db: AsyncSession, m: Member, doc, error: str = "", code: int = 200, content: str | None = None,
                   saved: bool = False, fresh: bool = False):
    job = (await db.get(Job, doc.job_id)) if doc.job_id else None
    original = resume_mod.text_of((await resume_mod.get_main(db, m)))
    shown = doc.content if content is None else content
    meta = doc.meta or {}
    return templates.TemplateResponse(request, "tailored.html", dict(
        org=None, doc=doc, job=job, error=error, saved=saved, fresh=fresh, content=shown, max_chars=tailor.MAX_EDIT_CHARS,
        check=tailor.review(original, shown) if original else {"unsupported": [], "dropped_years": [], "placeholders": tailor.placeholders(shown)},
        changes=meta.get("changes") or [], gaps=meta.get("gaps") or [], coverage=meta.get("coverage") or [],
        audit=meta.get("audit"), audit_done=("audit" in meta), corrected=meta.get("corrected") or []), status_code=code)


@app.get("/tailored", response_class=HTMLResponse)
async def tailored_list(request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/tailored", status_code=303)
    return templates.TemplateResponse(request, "tailored_list.html", dict(
        org=None, rows=(await tailor.listing(db, m)), has_resume=bool(resume_mod.text_of((await resume_mod.get_main(db, m))))))


@app.get("/tailored/{doc_id}", response_class=HTMLResponse)
async def tailored_view(doc_id: int, request: Request, db: AsyncSession = Depends(get_db), fresh: int = 0, saved: int = 0):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/tailored/{doc_id}", status_code=303)
    return await _tailored_page(request, db, m, (await _tailored_owned(db, m, doc_id)), fresh=bool(fresh), saved=bool(saved))


@app.post("/tailored/{doc_id}")
async def tailored_save(doc_id: int, request: Request, db: AsyncSession = Depends(get_db), content: str = Form("")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/tailored/{doc_id}", status_code=303)
    doc = await _tailored_owned(db, m, doc_id)
    try:
        await tailor.update(db, m, doc_id, content)
    except tailor.TailorError as e:
        return await _tailored_page(request, db, m, doc, error=str(e), code=e.status, content=content)
    return RedirectResponse(f"/tailored/{doc_id}?saved=1", status_code=303)


@app.post("/tailored/{doc_id}/delete")
async def tailored_delete(doc_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/tailored", status_code=303)
    await _tailored_owned(db, m, doc_id)
    await tailor.delete(db, m, doc_id)
    return RedirectResponse("/tailored", status_code=303)


@app.get("/tailored/{doc_id}/download")
async def tailored_download(doc_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/tailored/{doc_id}", status_code=303)
    doc = await _tailored_owned(db, m, doc_id)
    return PlainTextResponse(doc.content, headers={"Content-Disposition": f'attachment; filename="{tailor.filename(doc)}"'})


@app.get("/tailored/{doc_id}/print", response_class=HTMLResponse)
async def tailored_print(doc_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/tailored/{doc_id}", status_code=303)
    return templates.TemplateResponse(request, "tailored_print.html", dict(doc=(await _tailored_owned(db, m, doc_id))))


# ---------------------------------------------------------------- interview coach
async def _interview_list(request: Request, db: AsyncSession, m: Member, error: str = "", code: int = 200, upgrade: bool = False, form: dict | None = None):
    return templates.TemplateResponse(request, "interview_list.html", dict(
        org=None, rows=(await interview.listing(db, m)), ai_on=(await ai.enabled()), credits=(await ai.credits(db, m)), error=error, upgrade=upgrade,
        has_resume=bool(resume_mod.text_of((await resume_mod.get_main(db, m)))), form={"role": "", "company": "", "posting": "", **(form or {})}),
        status_code=code)


async def _interview_page(request: Request, db: AsyncSession, m: Member, s, error: str = "", code: int = 200, upgrade: bool = False,
                    drafts: dict | None = None):
    total, answered, fed = interview.progress(s)
    return templates.TemplateResponse(request, "interview.html", dict(
        org=None, s=s, job=(await db.get(Job, s.job_id)) if s.job_id else None, total=total, answered=answered, fed=fed, kinds=interview.KINDS,
        credits=(await ai.credits(db, m)), error=error, upgrade=upgrade, drafts=drafts or {}, max_answer=interview.MAX_ANSWER,
        generic=any(q.get("generic") for q in (s.questions or []))), status_code=code)


async def _interview_owned(db: AsyncSession, m: Member, sid: int):
    try:
        return await interview.get_owned(db, m, sid)
    except interview.InterviewError:
        raise HTTPException(404, "Not found")


@app.get("/interview", response_class=HTMLResponse)
async def interview_list(request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/interview", status_code=303)
    return await _interview_list(request, db, m)


@app.post("/interview/new")
async def interview_new(request: Request, db: AsyncSession = Depends(get_db), role: str = Form(""), company: str = Form(""), posting: str = Form("")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/interview", status_code=303)
    form = dict(role=role, company=company, posting=posting)
    if not (await ai.enabled()):
        return await _interview_list(request, db, m, "The interview coach is not available right now.", 503, form=form)
    throttle(request, "interview", 20, 3600)
    try:
        s = await interview.create_manual(db, m, role, company, posting)
    except interview.InterviewError as e:
        return await _interview_list(request, db, m, str(e), e.status, form=form)
    except ai.AIError as e:
        return await _interview_list(request, db, m, str(e), e.status, upgrade=e.status == 402, form=form)
    return RedirectResponse(f"/interview/{s.id}", status_code=303)


@app.post("/jobs/{job_id}/interview")
async def interview_for_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/jobs/{job_id}", status_code=303)
    job = await _visible_job(db, job_id)
    if not (await ai.enabled()):
        return await _interview_list(request, db, m, "The interview coach is not available right now.", 503)
    throttle(request, "interview", 20, 3600)
    try:
        s = await interview.create_for_job(db, m, job)
    except interview.InterviewError as e:
        return await _interview_list(request, db, m, str(e), e.status)
    except ai.AIError as e:
        return await _interview_list(request, db, m, str(e), e.status, upgrade=e.status == 402)
    return RedirectResponse(f"/interview/{s.id}", status_code=303)


@app.get("/interview/{sid}", response_class=HTMLResponse)
async def interview_view(sid: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/interview/{sid}", status_code=303)
    return await _interview_page(request, db, m, (await _interview_owned(db, m, sid)))


@app.post("/interview/{sid}/answer/{idx}")
async def interview_answer(sid: int, idx: int, request: Request, db: AsyncSession = Depends(get_db), answer: str = Form(""), action: str = Form("save")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/interview/{sid}", status_code=303)
    s = await _interview_owned(db, m, sid)
    try:
        if action == "feedback":
            if not (await ai.enabled()):
                return await _interview_page(request, db, m, s, "The interview coach is not available right now.", 503, drafts={idx: answer})
            throttle(request, "interview_fb", 60, 3600)
            await interview.get_feedback(db, m, sid, idx, answer)
        else:
            await interview.save_answer(db, m, sid, idx, answer)
    except interview.InterviewError as e:
        if e.status == 404:
            raise HTTPException(404, "Not found")
        return await _interview_page(request, db, m, (await _interview_owned(db, m, sid)), str(e), e.status, drafts={idx: answer})
    except ai.AIError as e:
        return await _interview_page(request, db, m, (await _interview_owned(db, m, sid)), str(e), e.status, upgrade=e.status == 402, drafts={idx: answer})
    return RedirectResponse(f"/interview/{sid}#q{idx}", status_code=303)


@app.post("/interview/{sid}/delete")
async def interview_delete(sid: int, request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/interview", status_code=303)
    await _interview_owned(db, m, sid)
    await interview.delete(db, m, sid)
    return RedirectResponse("/interview", status_code=303)


# ---------------------------------------------------------------- company pages (public)
@app.get("/companies", response_class=HTMLResponse)
async def companies_page(request: Request, db: AsyncSession = Depends(get_db), q: str = "", page: int = Query(1, ge=1, le=1000),
                         for_: str = Query("", alias="for")):
    me = await current_member(request, db)
    lens_ctx = await _lens_context(db, me, q, for_)
    total, rows = await companies.directory(db, q[:100], page, lens=lens_ctx["lens"])
    return templates.TemplateResponse(request, "companies.html", dict(
        lens_offer=lens_ctx["offer"], lens_on=lens_ctx["lens"] is not None, for_all=lens_ctx["for_all"],
        org=None, rows=rows, total=total, q=" ".join(q.split())[:100], page=page, pages=max(1, -(-total // companies.DIRECTORY_PER_PAGE))))


@app.get("/companies/{slug_id}", response_class=HTMLResponse)
async def company_page(slug_id: str, request: Request, db: AsyncSession = Depends(get_db), page: int = Query(1, ge=1, le=1000)):
    cid = companies.parse_id(slug_id)
    company = (await db.get(Company, cid)) if cid else None
    if not company or not company.active:
        raise HTTPException(404, "Company not found")
    main_record = await companies.canonical(db, company)
    want = companies.path(main_record)
    if request.url.path != want:                      # another record of the same company, or a stale or mistyped slug
        return RedirectResponse(want + (f"?{request.url.query}" if request.url.query else ""), status_code=301)
    ids = [c.id for c in (await companies.family(db, company))] or [company.id]
    me = await current_member(request, db)
    has_access = await _member_has_access(me, db)
    per = companies.PER_PAGE if has_access else config.PREVIEW_JOB_LIMIT
    total, rows = await companies.jobs_page(db, ids, page if has_access else 1, per)
    st = await companies.stats(db, ids)
    website = main_record.website if (main_record.website or "").startswith(("http://", "https://")) else ""
    return templates.TemplateResponse(request, "company.html", dict(
        org=None, company=main_record, st=st, jobs=rows, total=total, page=page if has_access else 1, pages=max(1, -(-total // per)),
        has_access=has_access, website=website, canonical_url=f"{config.PUBLIC_URL}{want}", now=datetime.utcnow(),
        following=(await follows.is_following(db, me, main_record)), signed_in=me is not None,
        sib=(await dedupe.locations_for(db, feed_query(), rows)), reactions=(await members.reaction_map(db, me))))


# ---------------------------------------------------------------- following companies
async def _company_or_404(db: AsyncSession, cid: int) -> Company:
    c = await db.get(Company, cid)
    if not c or not c.active:
        raise HTTPException(404, "Company not found")
    return c


@app.post("/companies/{cid}/follow")
async def company_follow(cid: int, request: Request, db: AsyncSession = Depends(get_db), next: str = Form("")):
    m = await current_member(request, db)
    company = await _company_or_404(db, cid)
    back = members.safe_next(next) if next else companies.path((await companies.canonical(db, company)))
    if not m:
        return RedirectResponse(f"/member/login?next={back}", status_code=303)
    throttle(request, "follow", 60, 3600)
    try:
        await follows.follow(db, m, company)
    except follows.FollowError as e:
        return await _following_page(request, db, m, error=str(e), code=e.status)
    return RedirectResponse(back, status_code=303)


@app.post("/companies/{cid}/unfollow")
async def company_unfollow(cid: int, request: Request, db: AsyncSession = Depends(get_db), next: str = Form("")):
    m = await current_member(request, db)
    company = await _company_or_404(db, cid)
    back = members.safe_next(next) if next else companies.path((await companies.canonical(db, company)))
    if not m:
        return RedirectResponse(f"/member/login?next={back}", status_code=303)
    await follows.unfollow(db, m, company)
    return RedirectResponse(back, status_code=303)


async def _following_page(request: Request, db: AsyncSession, m: Member, error: str = "", code: int = 200, msg: str = ""):
    return templates.TemplateResponse(request, "following.html", dict(
        org=None, rows=(await follows.followed(db, m)), recent=(await follows.recent(db, m)), enabled=(await follows.digest_enabled(db, m)), verified=bool(m.verified),
        error=error, msg=msg), status_code=code)


@app.get("/following", response_class=HTMLResponse)
async def following_page(request: Request, db: AsyncSession = Depends(get_db), msg: str = ""):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/following", status_code=303)
    return await _following_page(request, db, m, msg={"on": "Weekly email turned on.", "off": "Weekly email turned off."}.get(msg, ""))


@app.post("/following/digest")
async def following_digest(request: Request, db: AsyncSession = Depends(get_db), enabled: str = Form("1")):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/following", status_code=303)
    await follows.set_enabled(db, m, enabled == "1")
    return RedirectResponse("/following?msg=" + ("on" if enabled == "1" else "off"), status_code=303)


@app.get("/following/unsubscribe/{token}", response_class=HTMLResponse)
async def following_unsub_page(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    # a GET only asks for confirmation: mail scanners that open links must not unsubscribe anyone
    return templates.TemplateResponse(request, "follow_unsub.html", dict(valid=(await follows.member_for_token(db, token)) is not None, done=False))


@app.post("/following/unsubscribe/{token}", response_class=HTMLResponse)
async def following_unsub(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    m = await follows.member_for_token(db, token)     # also serves the List-Unsubscribe one-click POST
    if m:
        await follows.set_enabled(db, m, False)
    return templates.TemplateResponse(request, "follow_unsub.html", dict(valid=m is not None, done=m is not None))


# ---------------------------------------------------------------- salary pages (public)
@app.get("/salaries", response_class=HTMLResponse)
async def salaries_page(request: Request, db: AsyncSession = Depends(get_db), q: str = ""):
    return templates.TemplateResponse(request, "salaries.html", dict(
        org=None, rows=(await salaries.index(db, q[:100])), q=" ".join(q.split())[:100], min_samples=salaries.MIN_SAMPLES, min_companies=salaries.MIN_COMPANIES))


@app.get("/salaries/{slug}", response_class=HTMLResponse)
async def salary_page(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    g = await salaries.get(db, slug)
    if not g:
        raise HTTPException(404, "No salary page for that role")
    span = max(g["high"] - g["low"], 1)
    pos = lambda v: round(100 * (v - g["low"]) / span, 1)
    from urllib.parse import quote
    return templates.TemplateResponse(request, "salary.html", dict(
        org=None, g=g, left=pos(g["p25"]), width=max(pos(g["p75"]) - pos(g["p25"]), 1.5), med=pos(g["median"]),
        canonical_url=f"{config.PUBLIC_URL}/salaries/{g['slug']}", jobs_link=f"/jobs?q={quote(g['name'])}&paid=true&sort=pay",
        min_samples=salaries.MIN_SAMPLES, min_companies=salaries.MIN_COMPANIES))


# ---------------------------------------------------------------- browser extension lookup
@app.get("/api/v1/lookup")
async def api_lookup(request: Request, db: AsyncSession = Depends(get_db), title: str = "", company: str = "", location: str = ""):
    """Is the job someone is viewing on another site also on the employer's own career page? Takes no account and stores nothing."""
    throttle(request, "lookup", 120, 60)
    if not title.strip() or not company.strip():
        raise HTTPException(400, "title and company are required")
    result = await lookup_mod.find(db, title, company, location)
    return JSONResponse(result, headers={"Cache-Control": "private, max-age=300"})


# ---------------------------------------------------------------- referrals
@app.get("/r/{code}")
async def referral_link(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    """An invite link: remember who invited the visitor (only if they are not signed in yet) and show the homepage."""
    if not request.session.get("mid") and (await referrals.referrer_for(db, code[:20])):
        request.session["ref"] = code
    return RedirectResponse("/", status_code=303)


@app.get("/referrals", response_class=HTMLResponse)
async def referrals_page(request: Request, db: AsyncSession = Depends(get_db)):
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/referrals", status_code=303)
    s = await referrals.summary(db, m)
    return templates.TemplateResponse(request, "referrals.html", dict(org=None, s=s, link=f"{config.PUBLIC_URL.rstrip('/')}/r/{s['code']}",
                                                                      reward_days=referrals.REWARD_DAYS))


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
                 m: Member | None = Depends(current_member), msg: str = "",
                 billing_status: str = ""):
    if not m:
        return RedirectResponse("/member/login?next=/account", status_code=303)
    my_alerts = []
    if m.verified:
        try:
            my_alerts = list((await db.scalars(select(Subscriber).where(
                Subscriber.email == m.email,
                Subscriber.active.is_(True), Subscriber.confirmed.is_(True)))))
        except Exception:
            my_alerts = []
    from . import billing as billing_mod
    sub = await billing_mod.current_subscription(db, email=m.email)
    has_access = await _member_has_access(m, db)
    billing_msg = {
        "success": "Payment successful — you have full access.",
        "pending": "Payment is processing. Access will unlock shortly.",
        "failed": "Payment did not complete. Please try again.",
        "cancelled": "Subscription cancelled. Access continues until the end of the paid period.",
        "unknown": "We could not match that payment. Contact support if you were charged.",
    }.get(billing_status, "")
    return templates.TemplateResponse(request, "account.html", dict(
        org=org, member=m, alerts=my_alerts, msg=msg, error="",
        show_badge=False, sub=sub, has_access=has_access,
        billing_msg=billing_msg, billing_flash=billing_status,
        plans=config.CONSUMER_PLANS, billing_on=config.BILLING_ENABLED))


@app.post("/account/resend-verification")
async def account_resend(org: Org = Depends(tenant), m: Member | None = Depends(current_member)):
    if not m:
        return RedirectResponse("/member/login?next=/account", status_code=303)
    if not m.verified:
        await members.send_verification(org, m)
    return RedirectResponse("/account?msg=sent", status_code=303)


@app.post("/account/alerts/{sub_id}/unsubscribe")
async def account_unsub_alert(sub_id: int, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
                        m: Member | None = Depends(current_member)):
    if not m or not m.verified:
        return RedirectResponse("/account", status_code=303)
    sub = await db.get(Subscriber, sub_id)
    if sub and sub.email == m.email:
        sub.active = False
        await db.commit()
    return RedirectResponse("/account", status_code=303)


@app.post("/account/delete")
async def account_delete(request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
                   m: Member | None = Depends(current_member), password: str = Form("")):
    if not m:
        return RedirectResponse("/member/login", status_code=303)
    try:
        await members.delete_account(db, m, password)
    except members.MemberError as e:
        return templates.TemplateResponse(request, "account.html", dict(
            org=org, member=m, alerts=[], msg="", error=str(e), show_badge=_badge(org)), status_code=e.status)
    request.session.pop("mid", None)
    request.session.pop("memail", None)
    return _member_page(request, org, "Account deleted", "Your account and saved jobs were deleted. Any job alerts stay active until you unsubscribe from the link in the email.")


# ---------------------------------------------------------------- member job alerts (tenant host)
def _alert_page(request, org, title, body, status=200):
    return templates.TemplateResponse(request, "alert_msg.html", dict(
        org=org, title=title, body=body, show_badge=_badge(org)), status_code=status)


@app.post("/alerts", response_class=HTMLResponse)
async def alerts_subscribe(request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db),
                     email: str = Form(""), q: str = Form(""), loc: str = Form(""),
                     remote: bool = Form(False), frequency: str = Form("daily"), website: str = Form(""),
                     country: str = Form(""), type: str = Form(""), level: str = Form(""), paid: str = Form(""),
                     min_pay: str = Form(""), max_pay: str = Form(""), remote_from: str = Form(""), visa: str = Form("")):
    if website:  # honeypot field: real members never fill it
        return _alert_page(request, org, "Check your email", "We sent you a confirmation link.")
    throttle(request, "alerts", 20, 3600)
    if frequency == "hourly" and not (await _member_has_access((await current_member(request, db)), db)):
        return _alert_page(request, org, "Hourly alerts are for subscribers",
                           "Sign in with a paid plan to get hourly alerts, or choose daily or weekly. Daily and weekly alerts are free.", 403)
    me = await current_member(request, db)
    trusted = bool(me and me.verified and me.email == email.strip().lower())     # their own confirmed address: no double opt-in needed
    try:
        sub = await alerts.create_alert(db, org, email, q, loc, remote, frequency, trusted=trusted,
                                        filters=Filters.make(country, type, level, paid, min_pay, max_pay, remote_from, visa))
    except alerts.AlertError as e:
        return _alert_page(request, org, "Couldn't set up the alert", str(e), 400)
    except mailer.MailError:
        return _alert_page(request, org, "Couldn't send the email", "Please try again in a few minutes.", 502)
    if trusted:
        return _alert_page(request, org, "Alert is on", f"You'll get {sub.frequency} emails when new jobs match this search. Manage alerts on your account page.")
    if sub.confirmed:
        return _alert_page(request, org, "You're already subscribed", "You'll keep getting this alert.")
    return _alert_page(request, org, "Check your email", f"We sent a confirmation link to {email.strip().lower()}. Click it to start your {sub.frequency} alert.")


@app.get("/alerts/confirm/{token}", response_class=HTMLResponse)
async def alerts_confirm(token: str, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db)):
    sub = await alerts.confirm(db, token)
    if not sub:
        return _alert_page(request, org, "Link not valid", "This confirmation link is invalid or has expired.", 404)
    return _alert_page(request, org, "Alert confirmed", f"You'll get {sub.frequency} emails when new jobs match your search.")


@app.get("/alerts/unsubscribe/{token}", response_class=HTMLResponse)
def alerts_unsub_page(token: str, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db)):
    # GET only shows a confirm button: mail scanners that prefetch links must not unsubscribe people.
    return templates.TemplateResponse(request, "alert_unsub.html", dict(org=org, token=token, show_badge=_badge(org)))


@app.post("/alerts/unsubscribe/{token}", response_class=HTMLResponse)
async def alerts_unsub(token: str, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db)):
    await alerts.unsubscribe(db, token)  # also serves the List-Unsubscribe one-click POST
    return _alert_page(request, org, "Unsubscribed", "You won't receive this alert anymore.")


# ---------------------------------------------------------------- JSON API (optional public read; no white-label keys)
@app.get("/api/v1/jobs")
async def api_jobs(request: Request, db: AsyncSession = Depends(get_db),
             q: str = "", loc: str = "", remote: bool = False, days: int = 0,
             limit: int = Query(20, le=50), offset: int = 0, country: str = "", type: str = "",
             level: str = "", paid: str = "", min_pay: str = "", max_pay: str = "",
             remote_from: str = "", visa: str = "", sort: str = "new"):
    """Lightweight public job list. Rate-limited. Not a white-label embed product."""
    if not security.limiter.hit(f"api:{security.client_ip(request)}", 60, 60):
        raise HTTPException(429, "Rate limit exceeded")
    org = resolve_org(request, db)
    has_access = await _member_has_access((await current_member(request, db)), db)
    if not has_access:   # same cap as the web preview: first page only, no apply links
        offset, limit = 0, min(limit, config.PREVIEW_JOB_LIMIT)
    total, rows = await search_page(
        db, org, q, loc, remote, days, offset, limit,
        flt=Filters.make(country, type, level, paid, min_pay, max_pay, remote_from, visa),
        sort=sort if sort in SORTS else "new",
    )
    sib = await dedupe.locations_for(db, feed_query(org), rows)
    return {
        "total": total,
        "preview": not has_access,
        "jobs": [
            dict(
                id=j.id, title=j.title, company=j.company.name, location=j.location, remote=j.remote,
                apply_url=j.url if has_access else None, posted_at=j.posted_at, first_seen=j.first_seen,
                countries=[c for c in (j.countries or "").split("|") if c], job_type=j.job_type or None, seniority=j.seniority or None,
                workplace=j.workplace or None, remote_regions=[r for r in (j.remote_regions or "").split("|") if r],
                last_confirmed_open=j.last_seen, repost_count=j.repost_count or 0, visa_sponsorship=j.visa or None, salary_period=j.salary_period or None,
                pay_min_usd=j.pay_min_usd or None, pay_max_usd=j.pay_max_usd or None,
                locations=[l for _, l in sib.get(j.dup_key, [])] or ([j.location] if j.location else []),
            )
            for j in rows
        ],
    }


# ---------------------------------------------------------------- signup / auth (platform site)
def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


@app.api_route("/signup", methods=["GET", "POST"])
def signup_closed():
    """There is no public sign-up for platform admins. This route used to create an admin account for anyone who asked; operator
    accounts are now made only from the command line (`python -m admin create-admin`)."""
    raise HTTPException(404, "Not found")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, reset: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": "", "notice": "Password updated. Please sign in." if reset else ""})


@app.post("/login")
async def login(request: Request, db: AsyncSession = Depends(get_db), email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    ip = security.client_ip(request)
    key, ipkey = f"login:{ip}:{email}", f"loginip:{ip}"
    if security.limiter.blocked(key, 8, 900) or security.limiter.blocked(ipkey, 40, 900):
        return templates.TemplateResponse(request, "login.html", {"error": "Too many attempts. Please wait a few minutes and try again.", "notice": ""}, status_code=429)
    user = await db.scalar(select(User).where(User.email == email))
    if not user or not bcrypt.checkpw(password.encode()[:72], user.password_hash.encode()):
        security.limiter.record(key)
        security.limiter.record(ipkey)
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid email or password.", "notice": ""}, status_code=401)
    security.limiter.clear(key)
    start_admin_session(request, user)
    return RedirectResponse("/admin", status_code=303)


# ---- admin password reset (emailed, signed, single-use, 1 hour) and change
ADMIN_RESET_MAX_AGE = 3600
_admin_ser = URLSafeTimedSerializer(config.SECRET_KEY)


async def _admin_for_reset(db: AsyncSession, token: str) -> User | None:
    try:
        data = _admin_ser.loads(token, salt="admin-reset", max_age=ADMIN_RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = await db.get(User, data.get("u"))
    return user if user and user.password_hash[-12:] == data.get("h") else None   # dies the moment the password changes


@app.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return templates.TemplateResponse(request, "forgot.html", {"done": False})


@app.post("/forgot", response_class=HTMLResponse)
async def forgot(request: Request, db: AsyncSession = Depends(get_db), email: str = Form("")):
    throttle(request, "forgot", 10, 3600)
    email = email.strip().lower()
    user = (await db.scalar(select(User).where(User.email == email))) if email else None
    if user and security.limiter.hit(f"forgot:{email}", 3, 3600):   # at most 3 reset emails an hour per address
        tok = _admin_ser.dumps({"u": user.id, "h": user.password_hash[-12:]}, salt="admin-reset")
        link = f"{config.PUBLIC_URL}/reset/{tok}"
        try:
            await mailer.send(user.email, f"Reset your {config.PLATFORM_NAME} password",
                        f"Reset your password (link valid for 1 hour):\n\n{link}\n\nIf you didn't ask for this, ignore this email.",
                        f'<p>Reset your {config.PLATFORM_NAME} password. This link is valid for 1 hour.</p><p><a href="{link}">Choose a new password</a></p>'
                        "<p style='color:#666;font-size:12px'>If you didn't ask for this, ignore this email.</p>")
        except mailer.MailError:
            pass
    return templates.TemplateResponse(request, "forgot.html", {"done": True})


@app.get("/reset/{token}", response_class=HTMLResponse)
async def reset_form(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    if not (await _admin_for_reset(db, token)):
        raise HTTPException(404, "This reset link is invalid or has expired.")
    return templates.TemplateResponse(request, "reset.html", {"token": token, "error": ""})


@app.post("/reset/{token}")
async def reset_submit(token: str, request: Request, db: AsyncSession = Depends(get_db), password: str = Form("")):
    user = await _admin_for_reset(db, token)
    if not user:
        raise HTTPException(404, "This reset link is invalid or has expired.")
    err = "Password must be at least 8 characters." if len(password) < 8 else ("Password is too long (72 bytes at most)." if len(password.encode()) > 72 else "")
    if err:
        return templates.TemplateResponse(request, "reset.html", {"token": token, "error": err}, status_code=400)
    user.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    await db.commit()
    request.session.clear()
    return RedirectResponse("/login?reset=1", status_code=303)


@app.post("/admin/password")
async def admin_password(request: Request, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
                   current: str = Form(""), new: str = Form("")):
    err = ""
    if not bcrypt.checkpw(current.encode()[:72], user.password_hash.encode()):
        err = "Your current password is not correct."
    elif len(new) < 8:
        err = "The new password must be at least 8 characters."
    elif len(new.encode()) > 72:
        err = "The new password is too long (72 bytes at most)."
    if err:
        return templates.TemplateResponse(request, "admin.html", (await _admin_ctx(db, user, request, error=err)), status_code=400)
    user.password_hash = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
    await db.commit()
    start_admin_session(request, user)   # this session continues; every other session for this account ends
    return RedirectResponse("/admin?pw=changed#account", status_code=303)


@app.post("/admin/api-key/rotate")
def admin_rotate_api_key():
    return RedirectResponse("/admin", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- tenant admin console
async def _admin_ctx(db: AsyncSession, user: User, request: Request | None = None, **extra):
    """Minimal operator dashboard for the single B2C product (no employer / white-label)."""
    from .models import PLATFORM_ORG
    org = getattr(user, "org", None) or PLATFORM_ORG
    plan = getattr(org, "limits", None) or {}
    since = datetime.utcnow() - timedelta(days=30)
    try:
        clicks = (await db.scalar(select(func.count()).select_from(ClickEvent).where(ClickEvent.created_at >= since))) or 0
    except Exception:
        clicks = 0
    try:
        feed_total = (await db.scalar(select(func.count()).select_from(Job).where(Job.active.is_(True)))) or 0
    except Exception:
        feed_total = 0
    board_url = (config.PUBLIC_URL or "").rstrip("/") + "/jobs"
    ctx = dict(
        custom_target=getattr(config, "CUSTOM_DOMAIN_TARGET", ""),
        career_tools=[],
        tools_off=set(),
        ai_summary={},
        ai_by_tool={},
        ai_on=False,
        checklist=[],
        ob={},
        board_url=board_url,
        members=[user],
        invites=[],
        seats_used=1,
        direct_pending=0,
        default_age_days=config.DEFAULT_MAX_JOB_AGE_DAYS,
        seat_limit=1,
        subs_confirmed=0,
        digests=0,
        sub=None,
        billing_on=config.BILLING_ENABLED,
        paid_plans=getattr(billing, "PAID_PLANS", ()),
        yearly_months=getattr(config, "YEARLY_MONTHS", 12),
        now=datetime.utcnow(),
        billing_flash=request.query_params.get("billing", "") if request else "",
        user=user,
        org=org,
        plan=plan,
        own=[],
        clicks=clicks,
        top=[],
        feed_total=feed_total,
        ats_list=list(ATS) if ATS else [],
        base=getattr(config, "BASE_DOMAIN", ""),
        error="",
    )
    ctx.update(extra)
    return ctx

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse(request, "admin.html", (await _admin_ctx(db, user, request)))


@app.post("/admin/branding")
@app.post("/admin/domain/verify")
@app.post("/admin/curation")
@app.post("/admin/onboarding/dismiss")
@app.post("/admin/onboarding/show")
@app.post("/admin/onboarding/done/{key}")
@app.post("/admin/tools")
@app.post("/admin/team/invite")
@app.post("/admin/team/invites/{invite_id}/revoke")
@app.post("/admin/team/members/{user_id}/remove")
def admin_white_label_removed(key: str = "", invite_id: int = 0, user_id: int = 0):
    """White-label / multi-tenant admin actions removed in B2C pivot."""
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/companies")
async def admin_add_company(request: Request, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
                      ats: str = Form(...), board_token: str = Form(...), name: str = Form(...)):
    """Add an employer board to the global scrape registry."""
    from .scrapers import clean_token, ATS as ATS_MAP
    token = clean_token(ats, board_token)
    name = (name or "").strip()[:200]
    if not name or not token:
        return templates.TemplateResponse(
            request, "admin.html", (await _admin_ctx(db, user, request, error="Name and board token are required.")),
            status_code=400)
    existing = await db.scalar(select(Company).where(Company.ats == ats, Company.board_token == token))
    if existing:
        existing.active = True
        existing.name = name or existing.name
        await db.commit()
        return RedirectResponse("/admin?msg=updated", status_code=303)
    db.add(Company(name=name, ats=ats, board_token=token, active=True))
    await db.commit()
    return RedirectResponse("/admin?msg=added", status_code=303)


@app.post("/admin/companies/{cid}/delete")
async def admin_del_company(cid: int, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Deactivate a registry employer (does not delete historical jobs)."""
    c = await db.get(Company, cid)
    if c:
        c.active = False
        await db.commit()
    return RedirectResponse("/admin?msg=removed", status_code=303)


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(token: str, request: Request):
    return RedirectResponse("/jobs", status_code=303)



@app.post("/invite/{token}")
def invite_accept_removed(token: str):
    return RedirectResponse("/jobs", status_code=303)



# ---------------------------------------------------------------- résumé-aware board: profile lens, upload and the per-job coach
async def _lens_context(db: AsyncSession, member: Member | None, q: str, for_: str) -> dict:
    """What the board needs to filter by the member's résumé: the lens (None = show everything), whether one is on offer, and why not."""
    out = {"lens": None, "offer": None, "paused": False, "hint": False, "for_all": for_ == "all"}
    if not member:
        return out
    r = await resume_mod.get_main(db, member)
    if not r or not resume_mod.text_of(r):
        out["hint"] = True
        return out
    offer = lens_mod.Lens.from_profile((r.data or {}).get("profile"))
    out["offer"] = offer
    if offer is None:
        return out
    if (q or "").strip():
        out["paused"] = True                       # a specific search always wins over the résumé view
    elif not out["for_all"]:
        out["lens"] = offer
    return out


@app.get("/jobs/{job_id}/prepare", response_class=HTMLResponse)
async def prepare_application(job_id: int, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db)):
    """The application checklist for one job: coach → tailor → letter → interview → apply, with what is done and what each step costs."""
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/jobs/{job_id}/prepare", status_code=303)
    job = await db.scalar(feed_query(org).where(Job.id == job_id))
    if not job:
        raise HTTPException(404, "Job not found")
    return templates.TemplateResponse(request, "prepare.html", dict(
        org=org, job=job, plan=await prepare_mod.steps(db, m, job), credits=await ai.credits(db, m),
        has_access=await _member_has_access(m, db), ai_on=await ai.enabled()))


@app.post("/jobs/{job_id}/report-closed")
async def report_job_closed(job_id: int, request: Request, org: Org = Depends(tenant), db: AsyncSession = Depends(get_db)):
    """'This listing is closed': a member's report. Enough reports from different members hide the job."""
    m = await current_member(request, db)
    if not m:
        return RedirectResponse(f"/member/login?next=/jobs/{job_id}", status_code=303)
    throttle(request, "report", 30, 3600)
    job = await db.scalar(feed_query(org).where(Job.id == job_id))
    if not job:
        raise HTTPException(404, "Job not found")
    await reports.report_closed(db, m, job)
    return RedirectResponse(f"/jobs/{job_id}?reported=1", status_code=303)


@app.post("/alerts/from-resume")
async def alerts_from_resume(request: Request, db: AsyncSession = Depends(get_db)):
    """One daily alert per job title read off the member's résumé, so alerts need no search to be built by hand."""
    m = await current_member(request, db)
    if not m:
        return RedirectResponse("/member/login?next=/account", status_code=303)
    if not m.verified:
        return RedirectResponse("/account?msg=Confirm your email first, then turn on résumé alerts.", status_code=303)
    throttle(request, "alerts", 20, 3600)
    r = await resume_mod.get_main(db, m)
    profile = (r.data or {}).get("profile") if r else None
    made = await alerts.create_from_profile(db, m, profile)
    msg = f"Alerts are on for {len(made)} job title{'s' if len(made) != 1 else ''} from your résumé." if made else "Upload your résumé first so we know which jobs to watch for."
    return RedirectResponse("/account?msg=" + quote(msg), status_code=303)


async def _coach_context(db: AsyncSession, member: Member | None, job: Job) -> dict:
    """Résumé coach panel state for a job page."""
    ctx = {"coach": None, "coach_resume": False, "coach_credits": None, "coach_error": ""}
    if not member or not (await ai.enabled()):
        return ctx
    r = await resume_mod.get_main(db, member)
    ctx["coach_resume"] = bool(r and resume_mod.text_of(r))
    ctx["coach"] = coach_mod.cached(r, job.id) if ctx["coach_resume"] else None
    ctx["coach_credits"] = await ai.credits(db, member)
    return ctx


# ---------------------------------------------------------------- consumer billing (B2C)
def _after_payment(request: Request) -> str:
    """Where a person goes once paid: back to the job (or page) they came from, else the board. Always carries ?unlocked=1."""
    nxt = members.safe_next(request.session.pop("pay_next", "") or "/jobs")
    if not nxt.startswith("/jobs"):
        return nxt
    return nxt + ("&" if "?" in nxt else "?") + "unlocked=1" if "unlocked=" not in nxt else nxt


async def _remember_payer(request: Request, db: AsyncSession, reference: str) -> None:
    """The browser that completed a checkout has shown it controls that email's checkout, so that address counts as confirmed here."""
    co = await db.scalar(select(BillingCheckout).where(BillingCheckout.tx_ref == reference)) if reference else None
    if co and co.status == "paid" and co.customer_email:
        request.session["paid_email"] = co.customer_email.strip().lower()


@app.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request, plan: str = "monthly", next: str = "", db: AsyncSession = Depends(get_db)):
    """Choose a plan and start Paystack checkout (or show login if needed). `next` is where to return after paying (the job they were on)."""
    if plan not in billing.PAID_PLANS:
        plan = "monthly"
    if next:
        request.session["pay_next"] = members.safe_next(next)
    member = await current_member(request, db)
    info = config.CONSUMER_PLANS[plan]
    return templates.TemplateResponse(request, "checkout.html", {
        "plan": plan, "info": info, "plans": config.CONSUMER_PLANS,
        "member": member, "billing_on": config.BILLING_ENABLED,
        "error": "",
    })


@app.post("/checkout")
async def checkout_start(request: Request, db: AsyncSession = Depends(get_db),
                   plan: str = Form("monthly"), email: str = Form(""),
                   name: str = Form("")):
    if plan not in billing.PAID_PLANS:
        return RedirectResponse("/pricing", status_code=303)
    member = await current_member(request, db)
    email = (email or (member.email if member else "")).strip().lower()
    if not email:
        return templates.TemplateResponse(request, "checkout.html", {
            "plan": plan, "info": config.CONSUMER_PLANS[plan],
            "plans": config.CONSUMER_PLANS, "member": member,
            "billing_on": config.BILLING_ENABLED,
            "error": "Enter the email you want the subscription on.",
        }, status_code=400)

    if not config.BILLING_ENABLED:
        # Dev shortcut: grant access without Paystack when keys are missing
        if config.SECRET_KEY == "dev-only-change-me":
            from datetime import datetime, timedelta
            from .models import Subscription
            now = datetime.utcnow()
            for old in (await db.scalars(select(Subscription).where(
                Subscription.customer_email == email,
                Subscription.status.in_(["active", "cancelled"]),
            ))):
                old.status = "replaced"
            info = config.CONSUMER_PLANS[plan]
            sub = Subscription(
                plan=plan, interval=info["interval"], amount_cents=info["amount_pesewas"],
                currency=config.BILLING_CURRENCY, status="active",
                customer_email=email,
                current_period_start=now,
                current_period_end=billing.add_period(now, plan),
                last_payment_at=now,
            )
            db.add(sub)
            await db.commit()
            request.session["paid_email"] = email
            return RedirectResponse(_after_payment(request), status_code=303)
        return templates.TemplateResponse(request, "checkout.html", {
            "plan": plan, "info": config.CONSUMER_PLANS[plan],
            "plans": config.CONSUMER_PLANS, "member": member,
            "billing_on": False,
            "error": "Billing is not configured (set PAYSTACK_SECRET_KEY).",
        }, status_code=503)

    try:
        url = await billing.start_checkout(
            db, billing.get_client(), email, plan,
            name=name or email.split("@")[0],
        )
    except billing.BillingError as e:
        return templates.TemplateResponse(request, "checkout.html", {
            "plan": plan, "info": config.CONSUMER_PLANS[plan],
            "plans": config.CONSUMER_PLANS, "member": member,
            "billing_on": True, "error": str(e),
        }, status_code=e.status)
    return RedirectResponse(url, status_code=303)


@app.post("/checkout/session")
async def checkout_session(request: Request, db: AsyncSession = Depends(get_db),
                           plan: str = Form("monthly"), email: str = Form(""), name: str = Form("")):
    """Start a checkout for the in-page popup. Returns {reference, access_code, url} for the page to open Paystack with,
    or {error} with an HTTP status. The redirect flow (POST /checkout) stays as the fallback."""
    throttle(request, "cosession", 20, 600)
    if plan not in billing.PAID_PLANS:
        return JSONResponse({"error": "Unknown plan."}, status_code=400)
    if not config.BILLING_ENABLED:
        return JSONResponse({"error": "Billing is not configured."}, status_code=503)
    member = await current_member(request, db)
    email = (email or (member.email if member else "")).strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"error": "Enter the email you want the subscription on."}, status_code=400)
    try:
        url, reference = await billing.start_checkout_session(db, billing.get_client(), email, plan, name=name or email.split("@")[0])
    except billing.BillingError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return {"reference": reference, "access_code": billing.access_code_of(url), "url": url}


@app.get("/checkout/status")
async def checkout_status(request: Request, db: AsyncSession = Depends(get_db), reference: str = ""):
    """Polled by the checkout page while a payment is open. Verifies with Paystack and applies the payment (idempotent),
    so access is granted even where Paystack's webhook cannot reach this server."""
    throttle(request, "costatus", 120, 60)
    if not config.BILLING_ENABLED:
        return JSONResponse({"status": "unknown"}, status_code=503)
    try:
        out = await billing.checkout_status(db, billing.get_client(), reference)
    except Exception:
        logging.getLogger("careersniper").exception("checkout status failed")
        await db.rollback()
        return JSONResponse({"status": "pending", "provider": "error"}, status_code=200)
    if out["status"] == "paid":
        # access belongs to the email on the plan: a signed-in member goes straight back to what they were doing, a guest is sent to create the account
        await _remember_payer(request, db, reference)
        out["signed_in"] = bool(await current_member(request, db))
        dest = _after_payment(request)
        out["next"] = dest if out["signed_in"] else "/member/signup?next=" + quote(dest, safe="")
    return out


@app.post("/account/billing/cancel")
async def account_billing_cancel(request: Request, db: AsyncSession = Depends(get_db)):
    member = await current_member(request, db)
    if not member:
        return RedirectResponse("/member/login?next=/account", status_code=303)
    try:
        await billing.cancel_for_email(db, billing.get_client(), member.email)
    except billing.BillingError as e:
        return RedirectResponse(f"/account?billing_status=error", status_code=303)
    return RedirectResponse("/account?billing_status=cancelled", status_code=303)


@app.get("/billing/return")
async def billing_return(request: Request, db: AsyncSession = Depends(get_db),
                   reference: str = "", trxref: str = ""):
    """Browser redirect after Paystack checkout. Query params: reference (and trxref alias)."""
    throttle(request, "billret", 30, 60)
    def go(state):
        if state == "success":
            return RedirectResponse(_after_payment(request), status_code=303)
        return RedirectResponse(f"/account?billing_status={state}", status_code=303)

    ref = (reference or trxref or "").strip()
    if not ref:
        return go("unknown")
    co = await db.scalar(select(BillingCheckout).where(BillingCheckout.tx_ref == ref))
    if not co:
        return go("unknown")
    if co.status == "paid":
        await _remember_payer(request, db, ref)
        return go("success")
    try:
        ps = billing.get_client()
        tx = await ps.verify_transaction(ref)
        await billing.apply_verified_payment(db, ps, tx)
    except billing.PaystackError:
        return go("pending")  # webhook will finish
    await db.refresh(co)
    if co.status == "paid":
        await _remember_payer(request, db, ref)
    return go({"paid": "success", "pending": "pending"}.get(co.status, "failed"))


@app.post("/webhooks/paystack")
async def paystack_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Paystack webhook. Verify x-paystack-signature (HMAC SHA512 of raw body)."""
    raw = await request.body()
    sig = request.headers.get("x-paystack-signature", "")
    if not billing.verify_webhook_signature(raw, sig):
        return JSONResponse({"error": "Invalid signature."}, status_code=401)
    try:
        import json as _json
        body = _json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        body = {}
    event, data = body.get("event", ""), body.get("data") or {}
    try:
        ps = billing.get_client()
        if event == "charge.success":
            async def _apply():
                # Always re-verify by reference — never trust the body alone
                ref = data.get("reference") or ""
                tx = await ps.verify_transaction(ref)
                co = await db.scalar(select(BillingCheckout).where(
                    BillingCheckout.tx_ref == (tx.get("reference") or ref)))
                if co:   # a known checkout is never a renewal, even when its first webhook is delivered twice
                    return await billing.apply_verified_payment(db, ps, tx)
                return await billing.apply_renewal(db, ps, tx)
            res = await _apply()
        elif event == "subscription.create":
            customer = data.get("customer") or {}
            res = (await billing.apply_subscription_created(
                db, code=data.get("subscription_code") or "", email_token=data.get("email_token") or "",
                email=customer.get("email") if isinstance(customer, dict) else ""))
        elif event in ("subscription.disable", "subscription.not_renew"):
            customer = data.get("customer") or {}
            res = (await billing.apply_subscription_cancelled(
                db,
                sub_id=data.get("subscription_code") or data.get("id"),
                email=(customer.get("email") if isinstance(customer, dict) else None),
                plan_id=(data.get("plan") or {}).get("plan_code") if isinstance(data.get("plan"), dict) else data.get("plan"),
                event_id=str(data.get("id") or data.get("subscription_code") or ""),
            ))
        else:
            return {"ok": True, "ignored": "event not handled"}
        return {"ok": True, "outcome": res.get("outcome")}
    except billing.PaystackError as e:
        if e.http_status == 404:
            return {"ok": True, "ignored": "unknown transaction"}
        return JSONResponse({"error": "Could not process the event; please retry."}, status_code=500)
    except Exception:
        await db.rollback()
        return JSONResponse({"error": "Could not process the event; please retry."}, status_code=500)


@app.get("/health")
def health():
    """Liveness: the process is up. Cheap on purpose; use /ready for dependencies."""
    return {"ok": True}


@app.get("/ready")
async def ready():
    """Readiness: can we reach the database? 503 tells the load balancer to hold traffic."""
    try:
        await db_ping()
    except Exception:
        logging.getLogger("careersniper").exception("readiness check failed")
        return JSONResponse({"ok": False, "db": False}, status_code=503)
    return {"ok": True, "db": True}


# Career tools (resume/letters/interview/tracker) intentionally not registered.
# Discovery-first B2C product — tools can be re-enabled later under Account.


# ---------------------------------------------------------------- employer portal DISABLED (B2C seeker-only product)
# Multi-tenant employer posting is not part of the CareerHound-style consumer product.
# Routes redirect to /jobs so old bookmarks and links do not 404 with a portal UI.
@app.api_route("/employer", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/employer/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def employer_portal_removed(rest: str = ""):
    return RedirectResponse("/jobs", status_code=303)


@app.api_route("/tools", methods=["GET", "POST"])
@app.api_route("/tools/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def tools_removed(rest: str = ""):
    """AI/career tool hub removed from primary product surface."""
    return RedirectResponse("/jobs", status_code=303)


@app.api_route("/admin/employers", methods=["GET", "POST"])
@app.api_route("/admin/employers/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/admin/payments", methods=["GET", "POST"])
@app.api_route("/admin/payments/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def admin_employer_surface_removed(rest: str = ""):
    """Former employer-moderation UI — removed in B2C pivot."""
    return RedirectResponse("/admin", status_code=303)
