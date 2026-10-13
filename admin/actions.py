"""Everything the admin pages change. Each action checks its input, does the work with the site's own code, and writes an audit entry
in the same transaction, so there is never a change without a record of who made it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import ipaddress
import re
from urllib.parse import urlparse

from app import ai, billing, members as site_members, referrals, runtime_settings, scrapers
from app import config as site
from app.models import AdminAudit, Company, Job, Member, Referral, Subscription, User

from . import security

MAX_GRANT_DAYS = 365
ROLES = ("owner", "admin")


class AdminError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Admin:
    id: int
    email: str
    role: str

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def audit(db: AsyncSession, admin_email: str, action: str, target: str = "", detail: dict | None = None, ip: str = "") -> None:
    db.add(AdminAudit(admin_email=admin_email.lower(), action=action[:40], target=(target or "")[:200], detail=detail or None, ip=(ip or "")[:64]))


async def _member(db: AsyncSession, member_id: int) -> Member:
    m = await db.get(Member, member_id)
    if not m:
        raise AdminError("That member does not exist.", 404)
    return m


def _need_owner(admin: Admin) -> None:
    if not admin.is_owner:
        raise AdminError("Only an owner can do that.", 403)


# ---------------------------------------------------------------- members
async def verify_member(db: AsyncSession, admin: Admin, member_id: int, ip: str) -> str:
    m = await _member(db, member_id)
    if m.verified:
        return f"{m.email} is already confirmed."
    m.verified = True
    audit(db, admin.email, "member.verify", m.email, ip=ip)
    await db.commit()
    return f"Confirmed {m.email}."


async def grant_days(db: AsyncSession, admin: Admin, member_id: int, days: int, plan: str, note: str, ip: str) -> str:
    """Give free access: extends the member's live plan, or starts a free one (no payment, flagged as comped)."""
    m = await _member(db, member_id)
    if not 1 <= days <= MAX_GRANT_DAYS:
        raise AdminError(f"Give between 1 and {MAX_GRANT_DAYS} days.")
    if plan not in site.CONSUMER_PLANS:
        raise AdminError("Unknown plan.")
    note = " ".join((note or "").split())[:200]
    if not note:
        raise AdminError("Say why (a short note): it goes in the audit log.")
    now = datetime.utcnow()
    live = await billing.current_subscription(db, email=m.email)
    if live and live.current_period_end and live.current_period_end > now:
        if live.interval == "lifetime":
            raise AdminError("That member has a lifetime plan: there is nothing to add.")
        live.current_period_end += timedelta(days=days)
        how = f"extended their {live.plan} plan"
    else:
        info = site.CONSUMER_PLANS[plan]
        db.add(Subscription(plan=plan, interval=info["interval"], amount_cents=0, currency=site.BILLING_CURRENCY, status="active", customer_email=m.email.lower(),
                            current_period_start=now, current_period_end=now + timedelta(days=days)))
        how = f"started a free {plan}-level plan"
    audit(db, admin.email, "member.grant", m.email, {"days": days, "plan": plan, "note": note}, ip)
    await db.commit()
    return f"Gave {m.email} {days} days: {how}."


async def refund_member(db: AsyncSession, admin: Admin, member_id: int, ip: str) -> str:
    """Ends the member's paid access and withdraws any referral reward earned through them. The money is returned in the Paystack dashboard."""
    m = await _member(db, member_id)
    ended = await billing.refund_member(db, m.email)
    reward = await referrals.withdraw(db, m.email)
    audit(db, admin.email, "member.refund", m.email, {"plans_ended": ended, "referral_reward": reward}, ip)
    await db.commit()
    return f"Ended {ended} paid plan(s) for {m.email}; referral reward {reward}. Now return the money in the payment provider's dashboard."


async def delete_member(db: AsyncSession, admin: Admin, member_id: int, typed_email: str, ip: str) -> str:
    _need_owner(admin)
    m = await _member(db, member_id)
    if (typed_email or "").strip().lower() != m.email.lower():
        raise AdminError("Type the member's email address exactly to confirm.")
    email = m.email
    await site_members.purge_member(db, m)
    audit(db, admin.email, "member.delete", email, ip=ip)
    await db.commit()
    return f"Deleted {email} and everything attached to the account."


# ---------------------------------------------------------------- companies and jobs
async def add_company(db: AsyncSession, admin: Admin, name: str, ats: str, token: str, ip: str) -> str:
    name = " ".join((name or "").split())[:200]
    if not name:
        raise AdminError("Enter the company name.")
    if ats not in scrapers.ATS:
        raise AdminError("Choose one of the supported job-board systems.")
    try:
        token = scrapers.clean_token(ats, token)
    except ValueError as e:
        raise AdminError(str(e))
    if not token or len(token) > 120:
        raise AdminError("Enter the board token (the last part of the careers-page address).")
    if (await db.scalar(select(Company).where(Company.ats == ats, Company.board_token == token))):
        raise AdminError("That board is already in the registry.")
    c = Company(name=name, ats=ats, board_token=token, active=True)
    db.add(c)
    await db.flush()
    audit(db, admin.email, "company.add", f"{name} ({ats}/{token})", ip=ip)
    await db.commit()
    try:
        n = await scrapers.scrape_company(db, c)
    except Exception:
        n = 0
    await db.refresh(c)
    if c.last_error or not n:
        return f"Added {name}, but the first scrape found no jobs ({c.last_error[:120] or 'empty board'}). Check the token."
    return f"Added {name}: {n} jobs found."


async def toggle_company(db: AsyncSession, admin: Admin, company_id: int, ip: str) -> str:
    c = await db.get(Company, company_id)
    if not c:
        raise AdminError("That company does not exist.", 404)
    c.active = not c.active
    audit(db, admin.email, "company.activate" if c.active else "company.deactivate", f"{c.name} (#{c.id})", ip=ip)
    await db.commit()
    return f"{c.name} is now {'active' if c.active else 'switched off: its jobs are hidden and it is no longer scraped'}."


async def scrape_company_now(db: AsyncSession, admin: Admin, company_id: int, ip: str) -> str:
    c = await db.get(Company, company_id)
    if not c:
        raise AdminError("That company does not exist.", 404)
    try:
        n = await scrapers.scrape_company(db, c)
    except Exception as e:
        raise AdminError(f"The scrape failed: {type(e).__name__}.", 502)
    await db.refresh(c)
    audit(db, admin.email, "company.scrape", f"{c.name} (#{c.id})", {"jobs": n, "error": c.last_error[:120]}, ip)
    await db.commit()
    return f"{c.name}: {n} jobs." if not c.last_error else f"{c.name}: {c.last_error[:140]}"


async def set_job_removed(db: AsyncSession, admin: Admin, job_id: int, removed: bool, reason: str, ip: str) -> str:
    j = await db.get(Job, job_id)
    if not j:
        raise AdminError("That job does not exist.", 404)
    reason = " ".join((reason or "").split())[:200]
    if removed and not reason:
        raise AdminError("Say why you are removing it: it goes in the audit log.")
    j.status = "removed" if removed else "live"
    audit(db, admin.email, "job.remove" if removed else "job.restore", f"#{j.id} {j.title[:80]}", {"reason": reason} if removed else None, ip)
    await db.commit()
    return f"{'Removed' if removed else 'Restored'} “{j.title[:80]}”. {'It stays hidden even if the employer keeps it on their board.' if removed else ''}".strip()


# ---------------------------------------------------------------- referrals
async def withdraw_referral(db: AsyncSession, admin: Admin, referral_id: int, ip: str) -> str:
    r = await db.get(Referral, referral_id)
    if not r:
        raise AdminError("That referral does not exist.", 404)
    friend = await db.get(Member, r.referred_id)
    result = (await referrals.withdraw(db, friend.email)) if friend else "none"
    audit(db, admin.email, "referral.withdraw", f"referral #{r.id}", {"result": result}, ip)
    await db.commit()
    return "Reward withdrawn." if result == "withdrawn" else "There was no active reward to withdraw."


# ---------------------------------------------------------------- admins
async def create_admin(db: AsyncSession, admin: Admin | None, email: str, password: str, role: str, ip: str, by: str = "") -> User:
    """admin=None is the command line (the first owner). Otherwise only an owner may create admins."""
    if admin is not None:
        _need_owner(admin)
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 255 or " " in email:
        raise AdminError("Enter a valid email address.")
    if role not in ROLES:
        raise AdminError("Role must be owner or admin.")
    problem = security.password_problem(password)
    if problem:
        raise AdminError(problem)
    if (await db.scalar(select(User).where(func.lower(User.email) == email))):
        raise AdminError("An account with that email already exists.")
    u = User(email=email, role=role, password_hash=(await security.hash_password(password)))
    db.add(u)
    audit(db, admin.email if admin else (by or "command-line"), "admin.create", f"{email} ({role})", ip=ip)
    await db.commit()
    return u


async def disable_admin(db: AsyncSession, admin: Admin, user_id: int, ip: str) -> str:
    _need_owner(admin)
    u = await db.get(User, user_id)
    if not u or u.role not in ROLES:
        raise AdminError("That admin does not exist.", 404)
    if u.id == admin.id:
        raise AdminError("You cannot switch off your own account.")
    if u.role == "owner" and ((await db.scalar(select(func.count()).select_from(User).where(User.role == "owner"))) or 0) <= 1:
        raise AdminError("That is the last owner: the platform would be left with nobody who can manage admins.")
    u.role = "disabled"
    audit(db, admin.email, "admin.disable", u.email, ip=ip)
    await db.commit()
    return f"Switched off {u.email}. Their sessions end at their next click."


async def set_admin_password(db: AsyncSession, admin: Admin, user_id: int, password: str, ip: str) -> str:
    u = await db.get(User, user_id)
    if not u or u.role not in ROLES:
        raise AdminError("That admin does not exist.", 404)
    if u.id != admin.id:
        _need_owner(admin)
    problem = security.password_problem(password)
    if problem:
        raise AdminError(problem)
    u.password_hash = await security.hash_password(password)             # sessions carry a tag of the old hash, so they all end
    audit(db, admin.email, "admin.password", u.email, ip=ip)
    await db.commit()
    return f"Password changed for {u.email}. All their sessions have ended."


# ---------------------------------------------------------------- AI settings (platform-wide)
AI_PROVIDERS = ("anthropic", "openai", "openai_compatible")
TOKEN_PARAMS = ("max_tokens", "max_completion_tokens")
PLAN_KEYS = {"pass": "ai_requests_pass", "weekly": "ai_requests_weekly", "monthly": "ai_requests_monthly", "lifetime": "ai_requests_lifetime"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
AI_KEYS = tuple(k for k in runtime_settings.KEYS if k.startswith("ai_"))


def base_url_problem(url: str) -> str:
    """'' if the address is acceptable. Plain http is allowed only to this machine or an address on a private network:
    otherwise the API key and every resume would cross the internet unencrypted."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or len(url) > 300:
        return "The base address must look like https://api.example.com/v1 (no login in it)."
    if u.scheme == "http":
        host = u.hostname
        try:
            ip = ipaddress.ip_address(host)
            local = ip.is_loopback or ip.is_private
        except ValueError:
            local = host in ("localhost", "host.docker.internal")
        if not local:
            return "Use https:// for a server on the internet. Plain http is only for this machine or a private network address."
    return ""


def _number(raw, name: str, lo: float, hi: float, integer: bool, problems: list):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = int(raw) if integer else float(raw)
    except ValueError:
        problems.append(f"{name} must be a number.")
        return None
    if not lo <= v <= hi:
        lo_s, hi_s = (str(int(lo)), str(int(hi))) if integer else (f"{lo:g}", f"{hi:g}")      # %g would print a big whole number as 1e+06
        problems.append(f"{name} must be between {lo_s} and {hi_s}.")
        return None
    return v


def parse_ai_form(form: dict) -> tuple[dict, list[str]]:
    """(changes, problems). `changes` maps each AI setting to its new value, or None to go back to the environment's value.
    The API key is only in `changes` when a new one was typed or removal was asked for; blank means keep the stored one."""
    problems: list[str] = []
    ch: dict = {}
    ch["ai_enabled"] = None if form.get("enabled") == "on" else False
    provider = (form.get("provider") or "").strip()
    if provider and provider not in AI_PROVIDERS:
        problems.append("Choose one of the listed providers.")
        provider = ""
    ch["ai_provider"] = provider or None
    base = (form.get("base_url") or "").strip().rstrip("/")
    if base:
        p = base_url_problem(base)
        if p:
            problems.append(p)
            base = ""
    ch["ai_base_url"] = base or None
    model = (form.get("model") or "").strip()
    if model and (len(model) > 120 or _CONTROL.search(model)):
        problems.append("The model name looks wrong: it should be a short name such as gpt-4.1-mini.")
        model = ""
    ch["ai_model"] = model or None
    token_param = (form.get("token_param") or "").strip()
    if token_param and token_param not in TOKEN_PARAMS:
        problems.append("Choose one of the listed token settings.")
        token_param = ""
    ch["ai_token_param"] = token_param or None
    jm = (form.get("json_mode") or "").strip()
    if jm not in ("", "1", "0"):
        problems.append("JSON mode must be environment default, on or off.")
    ch["ai_json_mode"] = {"1": True, "0": False}.get(jm)
    ch["ai_timeout"] = _number(form.get("timeout"), "Timeout", 5, 300, False, problems)
    ch["ai_max_output_tokens"] = _number(form.get("max_output_tokens"), "Maximum answer length", 100, 8000, True, problems)
    ch["ai_max_concurrency"] = _number(form.get("max_concurrency"), "Calls at once", 1, 64, True, problems)
    ch["ai_free_requests"] = _number(form.get("free_trials"), "Free trial requests", 0, 10_000, True, problems)
    for plan, key in PLAN_KEYS.items():
        ch[key] = _number(form.get(f"daily_{plan}"), f"Daily requests on {plan}", 100, 100_000, True, problems)
    key = (form.get("api_key") or "").strip()
    if form.get("remove_key") == "on":
        ch["ai_api_key"] = None
    elif key:
        if not 8 <= len(key) <= 400 or re.search(r"\s", key) or _CONTROL.search(key):
            problems.append("That does not look like an API key (8 to 400 characters, no spaces).")
        else:
            ch["ai_api_key"] = key
    return ch, problems


async def _apply_ai(db: AsyncSession, admin: Admin, ch: dict) -> dict:
    updates = {k: v for k, v in ch.items() if v is not None}
    clears = [k for k, v in ch.items() if v is None]
    await runtime_settings.save(db, updates, clears, admin.email)
    return {"set": {k: v for k, v in updates.items() if k != "ai_api_key"}, "cleared": [k for k in clears if k != "ai_api_key"],
            "api_key": "replaced" if "ai_api_key" in updates else ("removed" if "ai_api_key" in clears else "unchanged")}


async def save_ai_settings(db: AsyncSession, admin: Admin, form: dict, ip: str) -> str:
    _need_owner(admin)
    ch, problems = parse_ai_form(form)
    if problems:
        raise AdminError(" ".join(problems))
    detail = await _apply_ai(db, admin, ch)
    audit(db, admin.email, "ai.settings", "platform", detail, ip)
    await db.commit()
    runtime_settings.invalidate()
    s, why = await ai.resolve()
    return "Saved. The site picks the change up within a few seconds. " + (f"AI is on: {s.label}." if s else f"AI is off: {why}")


async def test_ai(db: AsyncSession, admin: Admin, form: dict, ip: str) -> str:
    """Try the values on the form (saved or not) with one tiny real request. Nothing is saved."""
    _need_owner(admin)
    ch, problems = parse_ai_form(form)
    if problems:
        raise AdminError(" ".join(problems))
    s, why = await ai.resolve(dict(ch))          # None means "back to the environment", which is how the resolver reads it
    if not s:
        audit(db, admin.email, "ai.test", "platform", {"ok": False, "why": why}, ip)
        await db.commit()
        raise AdminError(f"These settings cannot work yet: {why}")
    try:
        result = await ai.check(s)
    except ai.AIError as e:
        audit(db, admin.email, "ai.test", "platform", {"ok": False, "provider": s.provider, "model": s.model, "error": str(e)[:200]}, ip)
        await db.commit()
        raise AdminError(f"The test failed: {e}")
    audit(db, admin.email, "ai.test", "platform", {"ok": True, "provider": s.provider, "model": s.model}, ip)
    await db.commit()
    return result


async def reset_ai_settings(db: AsyncSession, admin: Admin, ip: str) -> str:
    _need_owner(admin)
    await runtime_settings.save(db, {}, list(AI_KEYS), admin.email)
    audit(db, admin.email, "ai.reset", "platform", ip=ip)
    await db.commit()
    runtime_settings.invalidate()
    s, why = await ai.resolve()
    return "Back to the environment's settings. " + (f"AI is on: {s.label}." if s else f"AI is off: {why}")
