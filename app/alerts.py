"""Saved-search job alerts: double opt-in + digest sender (B2C, Resend)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import billing, config, dedupe, mailer
from .filters import NO_FILTERS, Filters
from .models import Job, Subscriber

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
MAX_ALERTS_PER_EMAIL = 5
CONFIRM_RESEND = timedelta(minutes=10)
# The scheduler ticks hourly, so "hourly" is due after 50 minutes. Hourly is a paid feature (hourly_allowed).
DUE = {"hourly": timedelta(minutes=50), "daily": timedelta(hours=23), "weekly": timedelta(days=6, hours=23)}


class AlertError(Exception):
    pass


def org_url(org=None) -> str:
    return config.PUBLIC_URL.rstrip("/")


async def hourly_allowed(db: AsyncSession, email: str) -> bool:
    """Hourly alerts are for people with a live paid plan (or any member while billing is off in development)."""
    return config.free_access() or (await billing.has_active_access(db, email=email))


def _describe(s: Subscriber) -> str:
    bits = [f'"{s.q}"' if s.q else "all new jobs"]
    if s.loc:
        bits.append(f"in {s.loc}")
    if s.remote:
        bits.append("(remote only)")
    bits += Filters.parse(s.filters or "").describe()
    return " ".join(bits)


async def create_alert(
    db: AsyncSession,
    org,
    email: str,
    q: str,
    loc: str,
    remote: bool,
    frequency: str,
    now: datetime | None = None,
    filters: Filters = NO_FILTERS,
    trusted: bool = False,
) -> Subscriber:
    """Create or refresh a saved-search alert. Sends confirmation email until confirmed."""
    now = now or datetime.utcnow()
    email = email.strip().lower()
    q, loc = " ".join(q.split())[:200], " ".join(loc.split())[:200]
    if not EMAIL_RE.match(email) or len(email) > 255:
        raise AlertError("Please enter a valid email address.")
    if frequency not in DUE:
        frequency = "daily"

    mine = list(
        (await db.scalars(
            select(Subscriber).where(Subscriber.email == email, Subscriber.active.is_(True))
        ))
    )
    same = next(
        (
            s
            for s in mine
            if (s.q.lower(), s.loc.lower(), s.remote, Filters.parse(s.filters or ""))
            == (q.lower(), loc.lower(), remote, filters)
        ),
        None,
    )
    if same:
        sub = same
        if sub.confirmed:
            sub.frequency = frequency
            await db.commit()
            return sub
    else:
        if len(mine) >= MAX_ALERTS_PER_EMAIL:
            raise AlertError(
                f"You already have {MAX_ALERTS_PER_EMAIL} alerts. Unsubscribe from one first."
            )
        sub = Subscriber(
            email=email,
            q=q,
            loc=loc,
            remote=remote,
            filters=filters.encode() if hasattr(filters, "encode") else "",
            frequency=frequency,
        )
        db.add(sub)
        await db.flush()

    if trusted:
        # the signed-in, email-confirmed member asked for it themselves: there is no mailbox to double-check
        sub.confirmed = True
        sub.frequency = frequency
        await db.commit()
        return sub
    if not sub.confirm_sent_at or now - sub.confirm_sent_at >= CONFIRM_RESEND:
        await _send_confirmation(sub)
        sub.confirm_sent_at = now
    await db.commit()
    return sub


async def _send_confirmation(sub: Subscriber) -> None:
    base = org_url()
    link = f"{base}/alerts/confirm/{sub.token}"
    name = config.PLATFORM_NAME
    text = (
        f"Confirm your {name} job alert: {_describe(sub)}, {sub.frequency}.\n\n"
        f"Confirm: {link}\n\n"
        "If you did not request this, ignore this email and nothing will be sent."
    )
    body = (
        f"<p>Confirm your <b>{escape(name)}</b> job alert:</p>"
        f"<p><b>{escape(_describe(sub))}</b> · {escape(sub.frequency)}</p>"
        + mailer.button(link, "Confirm alert")
        + "<p style='color:#6b7280;font-size:13px'>If you did not request this, ignore this email.</p>"
    )
    await mailer.send(
        sub.email,
        f"Confirm your job alert · {name}",
        text,
        mailer.wrap_html(body),
        from_name=name,
    )


async def confirm(db: AsyncSession, token: str) -> Subscriber | None:
    sub = await db.scalar(select(Subscriber).where(Subscriber.token == token, Subscriber.active.is_(True)))
    if not sub:
        return None
    if not sub.confirmed:
        sub.confirmed = True
        sub.last_checked_at = datetime.utcnow()
        await db.commit()
    return sub


async def unsubscribe(db: AsyncSession, token: str) -> Subscriber | None:
    sub = await db.scalar(select(Subscriber).where(Subscriber.token == token))
    if not sub:
        return None
    sub.active = False
    await db.commit()
    return sub


async def unsubscribe_by_id(db: AsyncSession, sub_id: int, email: str) -> bool:
    sub = await db.get(Subscriber, sub_id)
    if not sub or sub.email.lower() != email.strip().lower():
        return False
    sub.active = False
    await db.commit()
    return True


def _digest_message(sub: Subscriber, jobs: list, total: int):
    base = org_url() + "/jobs"
    unsub = f"{org_url()}/alerts/unsubscribe/{sub.token}"
    subject = f"{len(jobs)} new job{'s' if len(jobs) != 1 else ''} · {_describe(sub)}"
    lines, rows = [], []
    for j in jobs:
        where = f" · {j.location}" if j.location else ""
        link = f"{org_url()}/jobs/{j.id}"
        lines.append(f"- {j.title} at {j.company.name}{where}\n  {link}")
        rows.append(
            f'<p style="margin:0 0 12px">'
            f'<a href="{link}" style="font-weight:600;color:#2563eb;text-decoration:none">{escape(j.title)}</a><br>'
            f'<span style="color:#555">{escape(j.company.name)}{escape(where)}</span></p>'
        )
    text = (
        f"New jobs for {_describe(sub)}:\n\n"
        + "\n".join(lines)
        + f"\n\nSee all: {base}\nUnsubscribe: {unsub}"
    )
    html_body = (
        f"<p style='color:#555'>New jobs for <b>{escape(_describe(sub))}</b>:</p>"
        + "".join(rows)
        + (
            f"<p style='color:#555'>…and {total - len(jobs)} more on CareerSniper.</p>"
            if total > len(jobs)
            else ""
        )
        + mailer.button(base, "See all jobs")
        + f'<p style="color:#888;font-size:12px"><a href="{unsub}">Unsubscribe</a> from this alert.</p>'
    )
    html = mailer.wrap_html(html_body)
    headers = {
        "List-Unsubscribe": f"<{unsub}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    return subject, text, html, headers


async def run_digests(db: AsyncSession, now: datetime | None = None) -> dict:
    """Send due digests. Watermark advances only after success or empty check."""
    from .main import search  # lazy: avoid import cycle
    from .models import PLATFORM_ORG

    now = now or datetime.utcnow()
    stats = {"checked": 0, "sent": 0, "failed": 0}
    org = PLATFORM_ORG
    subs = (await db.scalars(
        select(Subscriber).where(Subscriber.confirmed.is_(True), Subscriber.active.is_(True))
    )).all()
    for sub in subs:
        if sub.frequency == "hourly" and not (await hourly_allowed(db, sub.email)):
            sub.frequency = "daily"      # the plan lapsed: keep the alert, at the free rate
            await db.commit()
        wm = sub.last_checked_at or sub.created_at
        if now - wm < DUE.get(sub.frequency, DUE["daily"]):
            continue
        stats["checked"] += 1
        q = search(org, sub.q, sub.loc, sub.remote, 0, flt=Filters.parse(sub.filters or "")).where(
            Job.first_seen > wm, Job.first_seen <= now
        )
        total, jobs = await dedupe.page(db, q, None, 0, config.DIGEST_MAX_JOBS)
        if total:
            try:
                subject, text, html, headers = _digest_message(sub, jobs, total)
                await mailer.send(
                    sub.email,
                    subject,
                    text,
                    html,
                    from_name=config.PLATFORM_NAME,
                    headers=headers,
                )
            except mailer.MailError:
                stats["failed"] += 1
                continue
            sub.last_sent_at = now
            sub.digests_sent = (sub.digests_sent or 0) + 1
            stats["sent"] += 1
        sub.last_checked_at = now
        await db.commit()
    return stats


async def create_from_profile(db: AsyncSession, member, profile, now: datetime | None = None) -> list[Subscriber]:
    """One daily alert per job title read off the member's résumé (up to 3), confirmed on the spot. Returns the alerts now active."""
    out: list[Subscriber] = []
    titles = [t for t in ((profile or {}).get("titles") or []) if isinstance(t, str) and t.strip()][:3]
    for t in titles:
        try:
            out.append(await create_alert(db, None, member.email, t, "", False, "daily", now=now, trusted=bool(member.verified)))
        except AlertError:
            break                                   # at the per-address cap: keep what was made
    return out
