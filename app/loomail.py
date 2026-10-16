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
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from . import billing, config
from .db import SessionLocal
from .models import Member, MemberProfile

log = logging.getLogger("careersniper")

TIMEOUT = httpx.Timeout(4.0)
_tasks: set = set()          # keeps background sends alive until they finish (a bare create_task can be garbage-collected)

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"Authorization": f"Bearer {config.LOOMAIL_SECRET_KEY}", "Content-Type": "application/json"})


async def _post(path: str, payload: dict) -> bool:
    """One call to Loomail, retried once on a network error, a 429 or a 5xx. True when it was accepted. Never raises."""
    url = config.LOOMAIL_URL.rstrip("/") + path
    for attempt in (1, 2):
        try:
            async with _client() as c:
                r = await c.post(url, json=payload)
            if r.status_code < 300:
                return True
            if r.status_code in (400, 401, 403, 404, 422):          # a mistake on our side: retrying cannot fix it
                log.warning("loomail %s refused: %s %s", path, r.status_code, r.text[:200])
                return False
        except Exception as e:                                       # noqa: BLE001  tracking must never break anything
            log.warning("loomail %s failed (%s)", path, type(e).__name__)
        if attempt == 1:
            await asyncio.sleep(0.5)
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
    if not await _post("/v1/identify", {"userId": uid, "traits": traits, "timestamp": _now()}):
        return
    if event:
        body = {"userId": uid, "event": event, "properties": properties or {}, "timestamp": _now()}
        if dedupe is not None:
            body["messageId"] = f"{event}:{uid}:{dedupe}"       # Loomail drops a repeat of the same messageId
        await _post("/v1/track", body)


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
    ok1 = await _post("/v1/identify", {"userId": "careersniper-selftest", "traits": {"plan": "free", "source": "careersniper-selftest"}})
    ok2 = await _post("/v1/track", {"userId": "careersniper-selftest", "event": "integration_test", "messageId": "careersniper-selftest"})
    return f"{'OK  ' if ok1 and ok2 else 'FAIL'} identify={'accepted' if ok1 else 'refused'}  track={'accepted' if ok2 else 'refused'}  at {config.LOOMAIL_URL}"
