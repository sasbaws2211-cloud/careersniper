"""Product events to Loomail (lifecycle email): who signed up, what they did, whether they pay.

Loomail turns events into email flows (welcome, activation rescue, win-back...). It needs a stable `userId` on every call, an `email`
trait for the person to be reachable, and event names you can build flows on. This module sends those from the server with the secret
key, so nothing depends on the visitor's browser (no ad-blockers, no consent banner games) and the key never reaches a page.

    emit(member.id, "onboarding_completed", {...})        fire and forget; a slow or down Loomail can never slow or break a page
    emit(email="a@b.c", event="paid", properties={...})    for places that only know an e-mail address (payments)

Switched on by `LOOMAIL_SECRET_KEY` (an `mf_live_...` key from Loomail → Integrate). With no key every call is a no-op.

What is sent about a person: their id, their e-mail (only once it is confirmed, so a mistyped or someone else's address never gets
marketing mail), plan and payment status, and the answers they gave in the questionnaire. No password, no résumé text.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from loomail import Loomail, LoomailError            # the official SDK (vendor/loomail-*.whl), not this module
from sqlalchemy import select

from . import billing, config
from .db import SessionLocal
from .models import Member, MemberProfile

log = logging.getLogger("careersniper")

TIMEOUT = 4.0                # seconds per attempt; the SDK retries once on a network error, a 429 or a 5xx, never on a 4xx
MAX_RETRIES = 1
_tasks: set = set()          # keeps background sends alive until they finish (a bare create_task can be garbage-collected)
# The SDK is blocking, so its calls run on their own small pool: a down Loomail can use up these 4 threads and nothing else,
# never the default executor the rest of the app shares.
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="loomail")

# what each paid AI tool is called in Loomail, so flows can speak about "tailored a résumé" rather than an internal tool name
TOOL_EVENTS = {
    "resume_coach": "fit_check_run",
    "tailor_resume": "resume_tailored",
    "cover_letter": "cover_letter_written",
    "interview_questions": "interview_practice_started",
    "match": "job_match_run",
}


def enabled() -> bool:
    return bool(config.LOOMAIL_SECRET_KEY)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _sdk() -> Loomail:
    """A client for the current settings (cheap to build, so a changed key or URL is picked up without a restart)."""
    return Loomail(config.LOOMAIL_SECRET_KEY, host=config.LOOMAIL_URL, timeout=TIMEOUT, max_retries=MAX_RETRIES)


async def _call(what: str, fn) -> bool:
    """Run one SDK call (`fn(client)`) off the event loop. True when Loomail accepted it. Never raises."""
    def work():
        return fn(_sdk())
    try:
        await asyncio.get_running_loop().run_in_executor(_pool, work)
        return True
    except LoomailError as e:
        log.warning("loomail %s failed: %s%s", what, e.code, f" ({e.status})" if e.status else "")
    except Exception as e:                                           # noqa: BLE001  tracking must never break anything
        log.warning("loomail %s failed (%s)", what, type(e).__name__)
    return False


async def traits_for(db, member: Member) -> dict:
    """What Loomail should know about this member right now. Reserved names (email, plan, payment_status) feed its lifecycle engine."""
    traits: dict = {"signed_up_at": member.created_at.replace(tzinfo=timezone.utc).isoformat(timespec="seconds") if member.created_at else None}
    if member.verified:
        traits["email"] = member.email             # only a confirmed address is ever mailed
    sub = await billing.current_subscription(db, email=member.email)
    if sub and sub.current_period_end and sub.current_period_end > datetime.utcnow():
        traits["payment_status"] = "cancelled" if sub.status == "cancelled" else "paid"
        traits["plan"] = sub.plan
        traits["plan_ends_at"] = sub.current_period_end.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    else:
        traits["payment_status"] = "cancelled" if sub else "free"
    from . import onboarding as onboarding_mod
    p = await onboarding_mod.get_profile(db, member)
    if p and p.completed_at:
        traits.update({"desired_titles": list(p.desired_titles or []), "experience_level": p.experience_level or None,
                       "country": p.work_country or None, "remote_preference": p.remote_preference or None,
                       "has_resume": p.has_resume, "onboarding_completed": True})
    return {k: v for k, v in traits.items() if v is not None}


async def _send(member_id: int | None, email: str | None, event: str | None, properties: dict | None, dedupe: str | None) -> None:
    async with SessionLocal() as db:
        member = await db.get(Member, member_id) if member_id else (await db.scalar(select(Member).where(Member.email == (email or "").strip().lower())))
        if member is None:
            return                                  # a payer who has no account yet: they are identified when they sign up
        traits = await traits_for(db, member)
    uid = str(member.id)
    if not await _call("identify", lambda c: c.identify(uid, traits, timestamp=_now())):
        return
    if event:
        # Loomail drops a repeat of the same message id; with no `dedupe` the SDK makes a fresh one per call (so a retry still can't double-count)
        message_id = f"{event}:{uid}:{dedupe}" if dedupe is not None else None
        await _call("track", lambda c: c.track(uid, event, properties or {}, timestamp=_now(), message_id=message_id))


def emit(member_id: int | None = None, event: str | None = None, properties: dict | None = None, *, email: str | None = None,
         dedupe: str | None = None) -> None:
    """Identify the member (fresh traits) and optionally record an event, in the background. Returns at once, whatever happens."""
    if not enabled() or not (member_id or email):
        return
    try:
        task = asyncio.get_running_loop().create_task(_send(member_id, email, event, properties, dedupe))
    except RuntimeError:                           # no running loop (a script): nothing to schedule
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def check() -> str:
    """For `python -m app.cli loomail-check`: send one test person and one test event, and say what Loomail answered."""
    if not enabled():
        return "LOOMAIL_SECRET_KEY is not set. Create a secret key in Loomail (Integrate) and put it in .env."
    ok1 = await _call("identify", lambda c: c.identify("careersniper-selftest", {"plan": "free", "source": "careersniper-selftest"}))
    ok2 = await _call("track", lambda c: c.track("careersniper-selftest", "integration_test", message_id="careersniper-selftest"))
    return f"{'OK  ' if ok1 and ok2 else 'FAIL'} identify={'accepted' if ok1 else 'refused'}  track={'accepted' if ok2 else 'refused'}  at {config.LOOMAIL_URL}"
