"""Where visitors drop off between the landing page and a payment.

Each step a browser takes is one row: a random per-browser id (kept in the signed session cookie), the step's name and a short detail.
There is no IP address, e-mail or search text in it. `report()` counts distinct browsers per step, and `python -m app.cli funnel` prints
the table with the share that carries on from each step to the next.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import FunnelEvent

log = logging.getLogger("careersniper")

# the main road, in order: what the report's conversion column follows
MAIN_STEPS = ("landing_view", "picker_used", "signup_done", "onb_done", "matches_view", "paywall_view", "checkout_start", "paid")
LABELS = {
    "landing_view": "Saw the landing page",
    "picker_used": "Used the hero search",
    "signup_done": "Created an account",
    "onb_done": "Finished the questionnaire",
    "matches_view": "Saw their matches",
    "paywall_view": "Hit the preview limit",
    "checkout_start": "Opened checkout",
    "paid": "Paid",
}
# steps the browser itself may report (the rest are recorded by the server, where they happen)
CLIENT_EVENTS = {"picker_used", "onb_step", "teaser_cta"}
_DETAIL = re.compile(r"^[a-z0-9_:.-]{0,40}$")
_BOT = re.compile(r"bot|crawl|spider|slurp|headless|preview|monitor|curl|python-requests|httpx", re.I)


def sid_of(request) -> str:
    """The browser's random id, made on first use. It lives in the signed session cookie, so it cannot be forged into another browser's."""
    sid = request.session.get("sid")
    if not sid:
        sid = secrets.token_hex(8)
        request.session["sid"] = sid
    return sid


def is_bot(request) -> bool:
    return bool(_BOT.search(request.headers.get("user-agent", "")) or not request.headers.get("user-agent"))


async def record(db: AsyncSession, request, event: str, detail: str = "", member_id: int | None = None, once: bool = False) -> None:
    """Write one step. `once` skips it when this browser already reported the same step (page reloads do not count twice).
    Never raises: tracking must not be able to break a page."""
    try:
        if is_bot(request) or not _DETAIL.match(detail or ""):
            return
        sid = sid_of(request)
        if once:
            have = await db.scalar(select(FunnelEvent.id).where(FunnelEvent.sid == sid, FunnelEvent.event == event, FunnelEvent.detail == (detail or "")).limit(1))
            if have:
                return
        db.add(FunnelEvent(sid=sid, event=event[:32], detail=(detail or "")[:40], member_id=member_id))
        await db.commit()
    except Exception:
        await db.rollback()
        log.warning("funnel event %s could not be recorded", event, exc_info=True)


async def report(db: AsyncSession, days: int = 30, now: datetime | None = None) -> dict:
    """{'since', 'steps': [(event, label, browsers, % of the first step, % of the step before)], 'onboarding': [(step, browsers)]}."""
    now = now or datetime.utcnow()
    since = now - timedelta(days=days)
    counts = {e: n for e, n in await db.execute(
        select(FunnelEvent.event, func.count(func.distinct(FunnelEvent.sid))).where(FunnelEvent.created_at >= since).group_by(FunnelEvent.event))}
    steps, first, prev = [], None, None
    for ev in MAIN_STEPS:
        n = counts.get(ev, 0)
        first = n if first is None else first
        steps.append((ev, LABELS[ev], n, round(100 * n / first) if first else 0, round(100 * n / prev) if prev else None))
        prev = n
    onboarding = [(d, n) for d, n in await db.execute(
        select(FunnelEvent.detail, func.count(func.distinct(FunnelEvent.sid))).where(FunnelEvent.created_at >= since, FunnelEvent.event == "onb_step")
        .group_by(FunnelEvent.detail).order_by(FunnelEvent.detail))]
    return {"since": since, "steps": steps, "onboarding": onboarding}


def render(rep: dict) -> str:
    """The report as plain text for the terminal."""
    lines = [f"Funnel since {rep['since']:%Y-%m-%d} (distinct browsers; each % is of the step before)", ""]
    for _ev, label, n, of_first, of_prev in rep["steps"]:
        step = f"{of_prev}% of previous" if of_prev is not None else ""
        lines.append(f"  {label:<30}{n:>7}   {of_first:>3}% of landing   {step}")
    if rep["onboarding"]:
        lines += ["", "Questionnaire steps reached:"] + [f"  step {d.replace('step', '') or '?':<4}{n:>7}" for d, n in rep["onboarding"]]
    return "\n".join(lines)
