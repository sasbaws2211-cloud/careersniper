"""Referral credit: a free week for each friend who subscribes.

The rules, which the referrals page also states:
  * every member has a personal link (/r/<code>); a visitor who arrives through it and then signs up is "referred";
  * when that friend completes a first paid purchase, the referrer gets 7 days: added to their current plan, or a free
    week of access if they have none. Nothing is given to the friend (a discount would clash with the refund promise);
  * no reward for referring yourself (the same address, ignoring dots and +tags), and at most MAX_REWARDS per person;
  * if the friend is refunded, the reward is withdrawn (`refund_member` + `withdraw`, used by `python -m app.cli refund`).
Granting is idempotent: a referral is rewarded at most once however many times the payment is reported.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import billing, config, mailer
from .models import Member, Referral, ReferralCode, Subscription

log = logging.getLogger("careersniper.referrals")
REWARD_DAYS = 7
REWARD_PLANS = ("weekly", "monthly", "lifetime")     # a 3-day pass is too small a purchase to earn a free week
MAX_REWARDS = 12
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"          # no look-alike characters
CODE_LENGTH = 8
GMAIL = {"gmail.com", "googlemail.com"}


def normalize_email(email: str) -> str:
    """The mailbox an address really delivers to, so a+1@gmail.com and a.b@gmail.com are not treated as different people."""
    local, _, domain = (email or "").strip().lower().partition("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def same_person(a: str, b: str) -> bool:
    return normalize_email(a) == normalize_email(b)


def mask(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return (local[:1] + "***@" + domain) if local else ""


# ---------------------------------------------------------------- codes and attribution
async def code_for(db: AsyncSession, member: Member) -> str:
    row = await db.scalar(select(ReferralCode).where(ReferralCode.member_id == member.id))
    if row:
        return row.code
    for _ in range(10):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        try:
            async with db.begin_nested():
                db.add(ReferralCode(member_id=member.id, code=code))
                await db.flush()
            await db.commit()
            return code
        except IntegrityError:
            await db.rollback()
            row = await db.scalar(select(ReferralCode).where(ReferralCode.member_id == member.id))
            if row:
                return row.code
    raise RuntimeError("could not make a referral code")


def valid_code(code: str) -> bool:
    return isinstance(code, str) and len(code) == CODE_LENGTH and all(c in ALPHABET for c in code)


async def referrer_for(db: AsyncSession, code: str) -> Member | None:
    if not valid_code(code):
        return None
    row = await db.scalar(select(ReferralCode).where(ReferralCode.code == code))
    return (await db.get(Member, row.member_id)) if row else None


async def attach(db: AsyncSession, new_member: Member, code: str | None) -> Referral | None:
    """Record that this new member came through a referral link. Quietly does nothing when the code is bad or it is the same person."""
    referrer = await referrer_for(db, code or "")
    if not referrer or referrer.id == new_member.id or same_person(referrer.email, new_member.email):
        return None
    if (await db.scalar(select(Referral.id).where(Referral.referred_id == new_member.id))):
        return None
    r = Referral(referrer_id=referrer.id, referred_id=new_member.id, code=code, status="signed_up")
    db.add(r)
    await db.commit()
    return r


# ---------------------------------------------------------------- the reward
async def _live_subscription(db: AsyncSession, email: str, now: datetime) -> Subscription | None:
    sub = await billing.current_subscription(db, email=email)
    return sub if sub and sub.current_period_end and sub.current_period_end > now else None


async def reward_for_payment(db: AsyncSession, payer_email: str, now: datetime | None = None, plan: str | None = None) -> str:
    """Call when a first purchase completes. Returns what happened: rewarded / none / self / capped / lifetime."""
    now = now or datetime.utcnow()
    if plan is not None and plan not in REWARD_PLANS:
        return "none"                                           # the referral stays open for a later, larger purchase
    referred = await db.scalar(select(Member).where(Member.email == (payer_email or "").strip().lower()))
    if not referred:
        return "none"
    ref = await db.scalar(select(Referral).where(Referral.referred_id == referred.id))
    if not ref or ref.status != "signed_up":
        return "none"                                           # not referred, or already dealt with: never a second reward
    referrer = await db.get(Member, ref.referrer_id)
    if not referrer or same_person(referrer.email, referred.email):
        ref.status = "skipped"
        await db.commit()
        return "self"
    earned = (await db.scalar(select(func.count()).select_from(Referral).where(Referral.referrer_id == referrer.id, Referral.status == "rewarded"))) or 0
    if earned >= MAX_REWARDS:
        ref.status = "capped"
        await db.commit()
        return "capped"
    sub = await _live_subscription(db, referrer.email, now)
    if sub and sub.interval == "lifetime":
        ref.status = "skipped"                                  # nothing to add to a plan that never ends
        await db.commit()
        return "lifetime"
    if sub:
        sub.current_period_end = sub.current_period_end + timedelta(days=REWARD_DAYS)
        ref.reward_sub_id, ref.reward_created_sub = sub.id, False
    else:
        free = Subscription(plan="weekly", interval="weekly", amount_cents=0, currency=config.BILLING_CURRENCY, status="active",
                            customer_email=referrer.email.lower(), current_period_start=now, current_period_end=now + timedelta(days=REWARD_DAYS))
        db.add(free)
        await db.flush()
        ref.reward_sub_id, ref.reward_created_sub = free.id, True
    ref.status, ref.reward_days, ref.rewarded_at = "rewarded", REWARD_DAYS, now
    await db.commit()
    await _tell_referrer(referrer)
    return "rewarded"


async def _tell_referrer(referrer: Member) -> None:
    base = config.PUBLIC_URL.rstrip("/")
    text = (f"A friend you invited just subscribed to {config.PLATFORM_NAME}, so we added {REWARD_DAYS} free days to your access. Thank you!\n\n"
            f"Your referral link and progress: {base}/referrals")
    html = mailer.wrap_html(f"<p>A friend you invited just subscribed, so we added <b>{REWARD_DAYS} free days</b> to your access. Thank you!</p>"
                            + mailer.button(f"{base}/referrals", "See your referrals"))
    try:
        await mailer.send(referrer.email, f"You earned {REWARD_DAYS} free days on {config.PLATFORM_NAME}", text, html, from_name=config.PLATFORM_NAME)
    except mailer.MailError:
        log.warning("could not tell referrer %s about their reward", referrer.id)


async def withdraw(db: AsyncSession, referred_email: str, now: datetime | None = None) -> str:
    """Take back the reward earned through this friend (they were refunded). Returns 'withdrawn' or 'none'."""
    now = now or datetime.utcnow()
    referred = await db.scalar(select(Member).where(Member.email == (referred_email or "").strip().lower()))
    ref = (await db.scalar(select(Referral).where(Referral.referred_id == referred.id))) if referred else None
    if not ref or ref.status != "rewarded":
        return "none"
    sub = (await db.get(Subscription, ref.reward_sub_id)) if ref.reward_sub_id else None
    if sub:
        if ref.reward_created_sub:
            sub.status, sub.current_period_end = "revoked", min(sub.current_period_end, now)
        else:
            sub.current_period_end = max(now, sub.current_period_end - timedelta(days=ref.reward_days or REWARD_DAYS))
    ref.status = "withdrawn"
    await db.commit()
    return "withdrawn"


# ---------------------------------------------------------------- the referrals page
async def summary(db: AsyncSession, member: Member) -> dict:
    rows = (await db.scalars(select(Referral).where(Referral.referrer_id == member.id).order_by(Referral.created_at.desc(), Referral.id.desc()))).all()
    friends = []
    for r in rows:
        f = await db.get(Member, r.referred_id)
        friends.append({"email": mask(f.email) if f else "(deleted account)", "status": r.status, "at": r.created_at})
    rewarded = sum(1 for r in rows if r.status == "rewarded")
    return {"code": (await code_for(db, member)), "signed_up": len(rows), "rewarded": rewarded, "days": rewarded * REWARD_DAYS, "friends": friends,
            "left": max(0, MAX_REWARDS - rewarded), "max": MAX_REWARDS}
