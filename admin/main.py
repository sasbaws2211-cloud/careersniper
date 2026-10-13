"""The admin web app. Run with `python -m admin run` (port 8030)."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app import config as site
from app import scrapers
from app import schema
from app.db import SessionLocal, get_db
from app.models import User

from . import actions, config, queries, security
from .actions import Admin, AdminError

BASE = Path(__file__).parent
log = logging.getLogger("careersniper.admin")
SESSION_COOKIE = "cs_admin"


@asynccontextmanager
async def lifespan(app):
    bad = config.problems()
    if bad and config.IS_PROD:
        raise RuntimeError("Refusing to start the admin app:\n  - " + "\n  - ".join(bad))
    for b in bad:
        log.warning("config: %s", b)
    if site.SCHEMA_MODE == "migrations" and not schema.is_current():
        raise RuntimeError(f"The database schema ({schema.current_revision() or 'empty'}) is behind this version of the app "
                           f"({schema.head_revision()}). Run `alembic upgrade head` first.")
    yield


app = FastAPI(title="CareerSniper admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY or "unset", session_cookie=SESSION_COOKIE, same_site="strict",
                   https_only=config.IS_PROD, max_age=int(config.SESSION_HOURS * 3600))
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ---------------------------------------------------------------- template helpers
def _dt(v, fmt="%d %b %Y %H:%M"):
    return v.strftime(fmt) if isinstance(v, datetime) else ""


def _ago(v):
    if not isinstance(v, datetime):
        return "never"
    secs = max(0, int((datetime.utcnow() - v).total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size * (1.5 if unit == "minute" else 1):
            n = secs // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


def _money(cents, currency="GHS"):
    return f"{currency} {(cents or 0) / 100:,.2f}"


templates.env.filters.update(dt=_dt, ago=_ago, money=_money)
templates.env.globals.update(site_url=site.PUBLIC_URL.rstrip("/"), now=datetime.utcnow, per_page=queries.PER_PAGE, ats_list=sorted(scrapers.ATS),
                             plans=list(site.CONSUMER_PLANS))


# ---------------------------------------------------------------- session, login state, CSRF
def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


async def current_admin(request: Request, db: AsyncSession = Depends(get_db)) -> Admin | None:
    s = request.session
    uid, iat, seen = s.get("uid"), s.get("iat"), s.get("seen")
    if not uid or not iat or not seen:
        return None
    now = time.time()
    if now - iat > config.SESSION_HOURS * 3600 or now - seen > config.IDLE_MINUTES * 60:
        s.clear()
        return None
    u = await db.get(User, uid)
    if not u or u.role not in actions.ROLES or s.get("ph") != u.password_hash[-10:]:     # switched off, or the password changed since this login
        s.clear()
        return None
    s["seen"] = now
    return Admin(id=u.id, email=u.email, role=u.role)


def require_admin(admin: Admin | None = Depends(current_admin)) -> Admin:
    if not admin:
        raise HTTPException(303, headers={"Location": "/login"})
    return admin


def csrf_token(request: Request) -> str:
    if not request.session.get("csrf"):
        request.session["csrf"] = security.new_token()
    return request.session["csrf"]


def check_csrf(request: Request, csrf: str = Form("")) -> None:
    """Every change is a POST that must carry the token from this session, and must come from this site."""
    if not security.tokens_match(csrf, request.session.get("csrf", "")):
        raise HTTPException(403, "That form has expired. Go back, reload the page and try again.")
    origin = request.headers.get("origin")
    if origin:
        allowed = {request.headers.get("host", ""), request.headers.get("x-forwarded-host", ""), *config.ALLOWED_HOSTS}
        if urlparse(origin).netloc not in allowed:
            log.warning("refused a form post: Origin %s is not Host %s (set ADMIN_ALLOWED_HOSTS if this is a proxy)", origin, request.headers.get("host"))
            raise HTTPException(403, "Cross-site request refused.")


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = (request.session.get("flash") or [])[-4:] + [[kind, message[:400]]]


def render(request: Request, name: str, admin: Admin | None = None, code: int = 200, now_flashes: list | None = None, **ctx):
    flashes = request.session.pop("flash", []) + (now_flashes or [])
    return templates.TemplateResponse(request, name, dict(admin=admin, csrf=csrf_token(request), flashes=flashes, nav=name.split(".")[0], **ctx), status_code=code)


async def do(request: Request, db: AsyncSession, fn, back: str):
    """Run one action, show its result on the next page, and go back (so a refresh never repeats it)."""
    try:
        flash(request, await fn())
    except AdminError as e:
        if e.status in (403, 404):
            raise HTTPException(e.status, str(e))
        await db.rollback()
        flash(request, str(e), "err")
    return RedirectResponse(back, status_code=303)


# ---------------------------------------------------------------- headers
@app.middleware("http")
async def headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update({
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store", "Permissions-Policy": "camera=(), microphone=(), geolocation=()", "X-Robots-Tag": "noindex, nofollow"})
    if config.IS_PROD:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code in (301, 302, 303, 307) and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path == "/health":
        return JSONResponse({"ok": False}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "error.html", dict(admin=None, csrf="", flashes=[], nav="", code=exc.status_code, message=str(exc.detail)), status_code=exc.status_code)


@app.get("/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- login and logout
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, admin: Admin | None = Depends(current_admin)):
    if admin:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error="")


@app.post("/login")
async def login(request: Request, db: AsyncSession = Depends(get_db), email: str = Form(""), password: str = Form("")):
    email, ip = email.strip().lower(), _ip(request)
    if security.locked(email, ip):
        actions.audit(db, email or "unknown", "login.locked", ip=ip)
        await db.commit()
        return render(request, "login.html", code=429, error="Too many attempts. Wait 15 minutes and try again.")
    u = (await db.scalar(select(User).where(func.lower(User.email) == email))) if email else None
    ok = await security.check_password(password, u.password_hash if u else None)
    if not (ok and u and u.role in actions.ROLES):
        security.record_failure(email, ip)
        actions.audit(db, email or "unknown", "login.failed", ip=ip)
        await db.commit()
        return render(request, "login.html", code=401, error="That email and password do not match an admin account.")
    security.clear_failures(email)
    request.session.clear()                                                     # a fresh session at login: nothing from before carries over
    now = time.time()
    request.session.update({"uid": u.id, "ph": u.password_hash[-10:], "iat": now, "seen": now, "csrf": security.new_token()})
    actions.audit(db, u.email, "login", ip=ip)
    await db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf)):
    actions.audit(db, admin.email, "logout", ip=_ip(request))
    await db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin)):
    return render(request, "dashboard.html", admin, d=(await queries.dashboard(db)), usd_to_ghs=site.USD_TO_GHS)


# ---------------------------------------------------------------- members
@app.get("/members", response_class=HTMLResponse)
async def members(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), q: str = "", verified: str = "", plan: str = "",
            page: int = Query(1, ge=1, le=100000)):
    total, rows = await queries.members_page(db, q[:100], verified, plan, page)
    return render(request, "members.html", admin, rows=rows, total=total, q=q[:100], verified=verified, plan=plan, page=page, pages=queries.pages(total))


@app.get("/members/{member_id}", response_class=HTMLResponse)
async def member(member_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin)):
    d = await queries.member_detail(db, member_id)
    if not d:
        raise HTTPException(404, "No such member")
    return render(request, "member.html", admin, d=d)


@app.post("/members/{member_id}/verify")
async def member_verify(member_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf)):
    return await do(request, db, lambda: actions.verify_member(db, admin, member_id, _ip(request)), f"/members/{member_id}")


@app.post("/members/{member_id}/grant")
async def member_grant(member_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                 days: int = Form(0), plan: str = Form("weekly"), note: str = Form("")):
    return await do(request, db, lambda: actions.grant_days(db, admin, member_id, days, plan, note, _ip(request)), f"/members/{member_id}")


@app.post("/members/{member_id}/refund")
async def member_refund(member_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf)):
    return await do(request, db, lambda: actions.refund_member(db, admin, member_id, _ip(request)), f"/members/{member_id}")


@app.post("/members/{member_id}/delete")
async def member_delete(member_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                  confirm_email: str = Form("")):
    try:
        message = await actions.delete_member(db, admin, member_id, confirm_email, _ip(request))
    except AdminError as e:
        if e.status in (403, 404):
            raise HTTPException(e.status, str(e))
        await db.rollback()
        flash(request, str(e), "err")
        return RedirectResponse(f"/members/{member_id}", status_code=303)
    flash(request, message)
    return RedirectResponse("/members", status_code=303)


# ---------------------------------------------------------------- billing
@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), tab: str = "payments", status: str = "",
                 page: int = Query(1, ge=1, le=100000)):
    tab = tab if tab in ("payments", "subscriptions", "events") else "payments"
    if tab == "payments":
        total, rows = await queries.payments_page(db, status, page)
        ids = await queries.member_ids_by_email(db, [r.customer_email for r in rows])
    elif tab == "subscriptions":
        total, rows = await queries.subscriptions_page(db, status or "live", page)
        ids = await queries.member_ids_by_email(db, [r.customer_email for r in rows])
    else:
        total, rows = await queries.events_page(db, page)
        ids = {}
    return render(request, "billing.html", admin, tab=tab, rows=rows, ids=ids, total=total, status=status, page=page, pages=queries.pages(total))


# ---------------------------------------------------------------- companies and scraping
@app.get("/companies", response_class=HTMLResponse)
async def companies(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), q: str = "", flt: str = "", page: int = Query(1, ge=1, le=100000)):
    total, rows = await queries.companies_page(db, q[:100], flt, page)
    return render(request, "companies.html", admin, rows=rows, total=total, q=q[:100], flt=flt, page=page, pages=queries.pages(total))


@app.post("/companies/add")
async def company_add(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                name: str = Form(""), ats: str = Form(""), token: str = Form("")):
    return await do(request, db, lambda: actions.add_company(db, admin, name, ats, token, _ip(request)), "/companies")


@app.post("/companies/{company_id}/toggle")
async def company_toggle(company_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                   back: str = Form("/companies")):
    return await do(request, db, lambda: actions.toggle_company(db, admin, company_id, _ip(request)), _safe_back(back, "/companies"))


@app.post("/companies/{company_id}/scrape")
async def company_scrape(company_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                   back: str = Form("/companies")):
    return await do(request, db, lambda: actions.scrape_company_now(db, admin, company_id, _ip(request)), _safe_back(back, "/companies"))


def _safe_back(back: str, default: str) -> str:
    return back if back.startswith("/") and not back.startswith("//") and "\\" not in back and "\n" not in back else default


# ---------------------------------------------------------------- jobs
@app.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), q: str = "", state: str = "live", page: int = Query(1, ge=1, le=100000)):
    total, rows = await queries.jobs_page(db, q[:100], state, page)
    return render(request, "jobs.html", admin, rows=rows, total=total, q=q[:100], state=state, page=page, pages=queries.pages(total))


@app.post("/jobs/{job_id}/remove")
async def job_remove(job_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
               reason: str = Form(""), back: str = Form("/jobs")):
    return await do(request, db, lambda: actions.set_job_removed(db, admin, job_id, True, reason, _ip(request)), _safe_back(back, "/jobs"))


@app.post("/jobs/{job_id}/restore")
async def job_restore(job_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                back: str = Form("/jobs?state=removed")):
    return await do(request, db, lambda: actions.set_job_removed(db, admin, job_id, False, "", _ip(request)), _safe_back(back, "/jobs?state=removed"))


# ---------------------------------------------------------------- AI, referrals, audit
@app.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin)):
    return render(request, "ai.html", admin, r=(await queries.ai_report(db)))


@app.get("/referrals", response_class=HTMLResponse)
async def referrals_page(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), status: str = "", page: int = Query(1, ge=1, le=100000)):
    total, rows = await queries.referrals_page(db, status, page)
    return render(request, "referrals.html", admin, rows=rows, total=total, status=status, page=page, pages=queries.pages(total))


@app.post("/referrals/{referral_id}/withdraw")
async def referral_withdraw(referral_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf)):
    return await do(request, db, lambda: actions.withdraw_referral(db, admin, referral_id, _ip(request)), "/referrals")


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), action: str = "", who: str = "", page: int = Query(1, ge=1, le=100000)):
    total, rows = await queries.audit_page(db, action[:40], who[:255], page)
    return render(request, "audit.html", admin, rows=rows, total=total, action=action[:40], who=who[:255], page=page, pages=queries.pages(total))


# ---------------------------------------------------------------- admins and system
@app.get("/admins", response_class=HTMLResponse)
async def admins_page(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin)):
    return render(request, "admins.html", admin, rows=(await queries.admins_list(db)), min_password=config.MIN_PASSWORD)


@app.post("/admins/create")
async def admin_create(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                 email: str = Form(""), password: str = Form(""), role: str = Form("admin")):
    async def go():
        await actions.create_admin(db, admin, email, password, role, _ip(request))
        return f"Created {email.strip().lower()}."
    return await do(request, db, go, "/admins")


@app.post("/admins/{user_id}/disable")
async def admin_disable(user_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf)):
    return await do(request, db, lambda: actions.disable_admin(db, admin, user_id, _ip(request)), "/admins")


@app.post("/admins/{user_id}/password")
async def admin_password(user_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                   password: str = Form("")):
    return await do(request, db, lambda: actions.set_admin_password(db, admin, user_id, password, _ip(request)), "/admins")


# ---------------------------------------------------------------- AI settings
@app.get("/settings/ai", response_class=HTMLResponse)
async def ai_settings(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin)):
    return render(request, "settings_ai.html", admin, view=(await queries.ai_settings_view(db)))


def _ai_form(enabled: str, provider: str, base_url: str, model: str, api_key: str, remove_key: str, token_param: str, json_mode: str, timeout: str,
             max_output_tokens: str, max_concurrency: str, free_trials: str, daily_pass: str, daily_weekly: str, daily_monthly: str, daily_lifetime: str) -> dict:
    return dict(enabled=enabled, provider=provider, base_url=base_url, model=model, api_key=api_key, remove_key=remove_key, token_param=token_param,
                json_mode=json_mode, timeout=timeout, max_output_tokens=max_output_tokens, max_concurrency=max_concurrency, free_trials=free_trials,
                daily_pass=daily_pass, daily_weekly=daily_weekly, daily_monthly=daily_monthly, daily_lifetime=daily_lifetime)


async def _ai_page(request: Request, db: AsyncSession, admin: Admin, form: dict, messages: list, code: int = 200):
    """The settings page again, with what was typed still in the form and the result shown straight away (nothing is saved by a test)."""
    ticket = security.stash(form["api_key"], admin.id) if form.get("api_key") else ""
    draft = {k: v for k, v in form.items() if k != "api_key"}
    return render(request, "settings_ai.html", admin, code=code, now_flashes=messages, view=(await queries.ai_settings_view(db)), draft=draft, draft_token=ticket,
                  draft_key=bool(ticket))


def _typed_key(form: dict, admin: Admin, draft_token: str) -> dict:
    """If the key box was left blank but a key typed a moment ago for a test is still held, use that one."""
    if not form["api_key"] and draft_token:
        held = security.unstash(draft_token, admin.id)
        if held:
            form["api_key"] = held
    return form


@app.post("/settings/ai")
async def ai_settings_save(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                     enabled: str = Form(""), provider: str = Form(""), base_url: str = Form(""), model: str = Form(""), api_key: str = Form(""),
                     remove_key: str = Form(""), token_param: str = Form(""), json_mode: str = Form(""), timeout: str = Form(""),
                     max_output_tokens: str = Form(""), max_concurrency: str = Form(""), free_trials: str = Form(""), daily_pass: str = Form(""),
                     daily_weekly: str = Form(""), daily_monthly: str = Form(""), daily_lifetime: str = Form(""), draft_token: str = Form("")):
    form = _typed_key(_ai_form(enabled, provider, base_url, model, api_key, remove_key, token_param, json_mode, timeout, max_output_tokens, max_concurrency,
                               free_trials, daily_pass, daily_weekly, daily_monthly, daily_lifetime), admin, draft_token)
    try:
        flash(request, (await actions.save_ai_settings(db, admin, form, _ip(request))))
    except AdminError as e:
        if e.status in (403, 404):
            raise HTTPException(e.status, str(e))
        await db.rollback()
        return await _ai_page(request, db, admin, form, [["err", str(e)]], 400)       # keep what was typed: do not make the admin start again
    return RedirectResponse("/settings/ai", status_code=303)


@app.post("/settings/ai/test")
async def ai_settings_test(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf),
                     enabled: str = Form(""), provider: str = Form(""), base_url: str = Form(""), model: str = Form(""), api_key: str = Form(""),
                     remove_key: str = Form(""), token_param: str = Form(""), json_mode: str = Form(""), timeout: str = Form(""),
                     max_output_tokens: str = Form(""), max_concurrency: str = Form(""), free_trials: str = Form(""), daily_pass: str = Form(""),
                     daily_weekly: str = Form(""), daily_monthly: str = Form(""), daily_lifetime: str = Form(""), draft_token: str = Form("")):
    form = _typed_key(_ai_form(enabled, provider, base_url, model, api_key, remove_key, token_param, json_mode, timeout, max_output_tokens, max_concurrency,
                               free_trials, daily_pass, daily_weekly, daily_monthly, daily_lifetime), admin, draft_token)
    try:
        messages = [["ok", (await actions.test_ai(db, admin, form, _ip(request)))]]
    except AdminError as e:
        if e.status in (403, 404):
            raise HTTPException(e.status, str(e))
        await db.rollback()
        messages = [["err", str(e)]]
    return await _ai_page(request, db, admin, form, messages)


@app.post("/settings/ai/reset")
async def ai_settings_reset(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin), _: None = Depends(check_csrf)):
    return await do(request, db, lambda: actions.reset_ai_settings(db, admin, _ip(request)), "/settings/ai")


@app.get("/system", response_class=HTMLResponse)
async def system(request: Request, db: AsyncSession = Depends(get_db), admin: Admin = Depends(require_admin)):
    return render(request, "system.html", admin, s=(await queries.system_info(db)), admin_problems=config.problems(), admin_env=config.ENV)
