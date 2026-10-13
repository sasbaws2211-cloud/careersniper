"""Member (job seeker) accounts on a tenant board: signup, email verification, password reset, saved jobs.

Members are separate from admin Users: scoped to one board (org), same email may exist on different boards.
Verify/reset links are signed, time-limited tokens (no extra tables). A reset token embeds the tail of the
current password hash, so it stops working as soon as the password changes (single use).
"""
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from html import escape

import asyncio

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config, mailer
from .alerts import EMAIL_RE
from . import config as _cfg
from .models import Member, Org, SavedJob

_ser = URLSafeTimedSerializer(config.SECRET_KEY)
VERIFY_MAX_AGE = 7 * 24 * 3600
RESET_MAX_AGE = 3600
MAX_PW_BYTES = 72   # bcrypt's limit


class MemberError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --- tiny in-memory login throttle (single-instance deployment; see README) ---------------
_attempts: dict[str, deque] = defaultdict(deque)
LOGIN_WINDOW, LOGIN_MAX = 15 * 60, 8


def throttled(key: str, now: float | None = None) -> bool:
    now = now or time.time()
    q = _attempts[key]
    while q and now - q[0] > LOGIN_WINDOW:
        q.popleft()
    return len(q) >= LOGIN_MAX


def record_failure(key: str, now: float | None = None) -> None:
    _attempts[key].append(now or time.time())


def clear_failures(key: str) -> None:
    _attempts.pop(key, None)


def safe_next(nxt: str) -> str:
    """Only same-site relative paths, to avoid open redirects."""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


async def _hash(pw: str) -> str:
    """bcrypt is slow on purpose, so it runs in a worker thread instead of stalling the event loop."""
    return await asyncio.to_thread(lambda: bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode())


async def _verify_pw(pw: str, hashed: str) -> bool:
    return await asyncio.to_thread(bcrypt.checkpw, pw.encode()[:MAX_PW_BYTES], hashed.encode())


def _check_pw(pw: str) -> None:
    if len(pw) < 8:
        raise MemberError("Password must be at least 8 characters.")
    if len(pw.encode()) > MAX_PW_BYTES:
        raise MemberError("Password is too long (72 bytes max).")


async def get(db: AsyncSession, org: Org | None, email: str) -> Member | None:
    return await db.scalar(select(Member).where(Member.email == email.strip().lower()))


# ------------------------------------------------------------------ signup / login
async def signup(db: AsyncSession, org: Org, email: str, password: str) -> Member:
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 255:
        raise MemberError("Please enter a valid email address.")
    _check_pw(password)
    if (await get(db, org, email)):
        raise MemberError("An account with that email already exists. Try logging in.")
    m = Member(email=email, password_hash=(await _hash(password)))
    db.add(m)
    await db.commit()
    await send_verification(org, m)
    return m


async def login(db: AsyncSession, org: Org, email: str, password: str) -> Member:
    m = await get(db, org, email)
    if not m or not await _verify_pw(password, m.password_hash):
        raise MemberError("Invalid email or password.", 401)
    return m


# ------------------------------------------------------------------ email verification
async def send_verification(org: Org, m: Member) -> None:
    name = (getattr(org, "name", None) if org else None) or _cfg.PLATFORM_NAME
    color = getattr(org, "brand_color", None) or "#2563eb"
    link = f"{_cfg.PUBLIC_URL}/member/verify/{_ser.dumps({'m': m.id, 'e': m.email}, salt='member-verify')}"
    text = "Confirm your email for %s.\n\n%s\n\nIf you did not sign up, you can ignore this email." % (name, link)
    body = (
        f"<p>Welcome! Confirm your email to finish setting up your <b>{escape(name)}</b> account.</p>"
        + mailer.button(link, "Confirm email", color)
        + "<p style='color:#6b7280;font-size:13px'>If you did not sign up, you can ignore this email.</p>"
    )
    html = mailer.wrap_html(body, brand_color=color)
    try:
        await mailer.send(m.email, f"Confirm your email · {name}", text, html, from_name=name)
    except mailer.MailError:
        pass  # account works without it; resend from account page


async def verify(db: AsyncSession, org: Org, token: str) -> Member | None:
    try:
        data = _ser.loads(token, salt="member-verify", max_age=VERIFY_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    m = await db.get(Member, data.get("m"))
    if not m or m.email != data.get("e"):
        return None
    if not m.verified:
        m.verified = True
        await db.commit()
    return m


# ------------------------------------------------------------------ password reset
async def send_reset(db: AsyncSession, org: Org, email: str) -> None:
    """Always silent about whether the email exists (no account enumeration)."""
    m = await get(db, org, email)
    if not m:
        return
    name = (getattr(org, "name", None) if org else None) or _cfg.PLATFORM_NAME
    color = getattr(org, "brand_color", None) or "#2563eb"
    link = f"{_cfg.PUBLIC_URL}/member/reset/{_ser.dumps({'m': m.id, 'h': m.password_hash[-12:]}, salt='member-reset')}"
    text = "Reset your %s password (link valid for 1 hour):\n\n%s\n\nIf you did not ask for this, ignore this email." % (name, link)
    body = (
        f"<p>Reset your <b>{escape(name)}</b> password. This link expires in 1 hour.</p>"
        + mailer.button(link, "Choose a new password", color)
        + "<p style='color:#6b7280;font-size:13px'>If you did not ask for this, ignore this email.</p>"
    )
    html = mailer.wrap_html(body, brand_color=color)
    try:
        await mailer.send(m.email, f"Reset your password · {name}", text, html, from_name=name)
    except mailer.MailError:
        pass


async def _reset_member(db: AsyncSession, org: Org, token: str) -> Member | None:
    """The member a reset token belongs to, or None if it is forged, expired or already used (password changed)."""
    try:
        data = _ser.loads(token, salt="member-reset", max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    m = await db.get(Member, data.get("m"))
    if not m or m.password_hash[-12:] != data.get("h"):
        return None
    return m


async def reset_token_valid(db: AsyncSession, org: Org, token: str) -> bool:
    return (await _reset_member(db, org, token)) is not None


async def reset_password(db: AsyncSession, org: Org, token: str, password: str) -> Member:
    m = await _reset_member(db, org, token)
    if not m:
        raise MemberError("This reset link is invalid or has expired.", 404)
    _check_pw(password)
    m.password_hash = await _hash(password)
    m.verified = True   # they proved control of the inbox
    await db.commit()
    return m


# ------------------------------------------------------------------ reactions (like / dislike / save)
# status values: liked | disliked | saved | applied
# "liked" and "saved" are both positive; liked is the primary thumbs-up reaction.

VALID_REACTIONS = ("liked", "disliked", "saved", "applied")


async def set_reaction(db: AsyncSession, m: Member, job_id: int, reaction: str) -> str | None:
    """Set or clear a reaction. Pass the same reaction again to clear.
    Returns the new status, or None if cleared."""
    if reaction not in VALID_REACTIONS:
        raise MemberError("Invalid reaction.")
    row = await db.scalar(select(SavedJob).where(SavedJob.member_id == m.id, SavedJob.job_id == job_id))
    if row and row.status == reaction:
        await db.delete(row)
        await db.commit()
        return None
    if row:
        row.status = reaction
    else:
        db.add(SavedJob(member_id=m.id, job_id=job_id, status=reaction))
    await db.commit()
    return reaction


async def toggle_saved(db: AsyncSession, m: Member, job_id: int) -> bool:
    """Legacy heart toggle → liked. Returns True if now liked."""
    return (await set_reaction(db, m, job_id, "liked")) == "liked"


async def set_status(db: AsyncSession, m: Member, job_id: int, status: str) -> None:
    if status not in VALID_REACTIONS:
        return
    await set_reaction(db, m, job_id, status)


async def reaction_map(db: AsyncSession, m: Member | None) -> dict[int, str]:
    """job_id → status for this member."""
    if not m:
        return {}
    rows = (await db.execute(select(SavedJob.job_id, SavedJob.status).where(SavedJob.member_id == m.id))).all()
    return {jid: status for jid, status in rows}


async def saved_ids(db: AsyncSession, m: Member | None) -> set[int]:
    """Positive reactions (liked or saved) — used by older templates."""
    if not m:
        return set()
    return set((await db.scalars(select(SavedJob.job_id).where(
        SavedJob.member_id == m.id,
        SavedJob.status.in_(("liked", "saved", "applied")),
    ))))


async def disliked_ids(db: AsyncSession, m: Member | None) -> set[int]:
    if not m:
        return set()
    return set((await db.scalars(select(SavedJob.job_id).where(
        SavedJob.member_id == m.id, SavedJob.status == "disliked"))))


async def delete_account(db: AsyncSession, m: Member, password: str) -> None:
    if not await _verify_pw(password, m.password_hash):
        raise MemberError("That password is not correct.", 403)
    await purge_member(db, m)


async def purge_member(db: AsyncSession, m: Member) -> None:
    """Remove a member and everything that belongs to them (also used by the admin app, which has no password to check)."""
    from .models import AiUsage, Application, CompanyFollow, Document, FollowDigest, InterviewSession, Referral, ReferralCode, Resume, Subscriber
    for model in (SavedJob, Application, Resume, Document, InterviewSession, AiUsage, CompanyFollow, FollowDigest, ReferralCode):
        await db.execute(delete(model).where(model.member_id == m.id))
    await db.execute(delete(Referral).where((Referral.referrer_id == m.id) | (Referral.referred_id == m.id)))
    await db.execute(delete(Subscriber).where(Subscriber.email == m.email))      # their job alerts go too
    await db.delete(m)
    await db.commit()
