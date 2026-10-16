"""Win-back email: a plan that will not renew ends soon, so say what was found while it ran."""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config, mailer
from .billing import ONE_TIME_INTERVALS, _record_event
from .models import BillingEvent, Job, Subscription

WINDOW = timedelta(hours=26)       # remind once, a little over a day before the end


async def due(db: AsyncSession, now: datetime) -> list[Subscription]:
    """Plans that end inside the window and will not renew themselves (a pass, a cancelled plan)."""
    q = select(Subscription).where(
        Subscription.status.in_(["active", "cancelled"]),
        Subscription.current_period_end > now, Subscription.current_period_end <= now + WINDOW)
    out = []
    for s in (await db.scalars(q)):
        if s.plan == "lifetime":
            continue
        if s.status == "active" and s.interval not in ONE_TIME_INTERVALS:
            continue                               # recurring and still on: it renews, nothing to say
        out.append(s)
    return out


def _message(sub: Subscription, new_jobs: int):
    name = config.PLATFORM_NAME
    link = f"{config.PUBLIC_URL.rstrip('/')}/pricing"
    when = f"{sub.current_period_end:%d %b}"
    lines = f"Your {name} access ends on {when} (UTC)."
    if new_jobs:
        lines += f" {new_jobs:,} new jobs were added while you were a member."
    text = f"{lines}\n\nKeep your alerts, tracker and AI tools working: {link}\n"
    body = (f"<p>Your <b>{escape(name)}</b> access ends on <b>{when}</b>.</p>"
            + (f"<p>{new_jobs:,} new jobs were added while you were a member.</p>" if new_jobs else "")
            + "<p>Keep your alerts, tracker and AI tools working.</p>" + mailer.button(link, "Keep my access"))
    return f"Your {name} access ends {when}", text, mailer.wrap_html(body)


async def run(db: AsyncSession, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    stats = {"due": 0, "sent": 0}
    for sub in (await due(db, now)):
        stats["due"] += 1
        key = f"expiry_reminder:{sub.id}:{sub.current_period_end:%Y%m%d}"
        if not (await _record_event(db, key, "expiry_reminder", "claimed", {"email": sub.customer_email})):
            await db.rollback()
            continue                               # already reminded for this period
        await db.commit()
        from . import loomail
        loomail.emit(email=sub.customer_email, event="plan_expiring", dedupe=f"{sub.id}:{sub.current_period_end:%Y%m%d}",
                     properties={"plan": sub.plan, "ends": f"{sub.current_period_end:%Y-%m-%d}"})
        new_jobs = (await db.scalar(select(func.count()).select_from(Job).where(Job.first_seen >= (sub.current_period_start or now - timedelta(days=7))))) or 0
        try:
            subject, text, html = _message(sub, new_jobs)
            await mailer.send(sub.customer_email, subject, text, html, from_name=config.PLATFORM_NAME)
            stats["sent"] += 1
        except mailer.MailError:
            await db.execute(delete(BillingEvent).where(BillingEvent.event_key == key))   # not sent: let the next tick try again
            await db.commit()
    return stats
