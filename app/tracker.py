"""Application tracker: the jobs a member is pursuing, from saved to offer. Plain bookkeeping, no AI.

A member sees and changes only their own rows: every function takes the member and filters on it.
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Application, Job, Member

STATUSES = ("saved", "applied", "interviewing", "offer", "rejected")
LABELS = {"saved": "Saved", "applied": "Applied", "interviewing": "Interviewing", "offer": "Offer", "rejected": "Rejected / closed"}
MAX_PER_MEMBER = 500
LIMITS = {"company": 200, "title": 300, "url": 1000, "notes": 5000}


class TrackerError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _clip(value: str, field: str) -> str:
    return " ".join((value or "").split())[: LIMITS[field]] if field != "notes" else (value or "").replace("\r\n", "\n").strip()[: LIMITS[field]]


def clean_url(raw: str) -> str:
    """'' or an http(s)/mailto link. Anything else (javascript:, data: ...) would become a clickable link on the page."""
    url = (raw or "").strip()
    if not url:
        return ""
    if len(url) > LIMITS["url"]:
        raise TrackerError("That link is too long.")
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return url
    if parsed.scheme == "mailto" and parsed.path:
        return url
    raise TrackerError("The link must start with http:// or https://.")


def _check_status(status: str) -> str:
    if status not in STATUSES:
        raise TrackerError("Unknown status.")
    return status


async def _count(db: AsyncSession, member: Member) -> int:
    return (await db.scalar(select(func.count()).select_from(Application).where(Application.member_id == member.id))) or 0


async def _room(db: AsyncSession, member: Member) -> None:
    if (await _count(db, member)) >= MAX_PER_MEMBER:
        raise TrackerError(f"You are tracking {MAX_PER_MEMBER} applications, the most we keep. Delete some old ones first.")


def _stamp(app: Application, status: str, now: datetime) -> None:
    """Moving past 'saved' means you applied: remember when, the first time only."""
    if status != "saved" and not app.applied_on:
        app.applied_on = now


async def add_manual(db: AsyncSession, member: Member, company: str, title: str, url: str = "", status: str = "applied", notes: str = "") -> Application:
    company, title = _clip(company, "company"), _clip(title, "title")
    if not company or not title:
        raise TrackerError("Enter the company and the job title.")
    _check_status(status)
    await _room(db, member)
    now = datetime.utcnow()
    app = Application(member_id=member.id, company=company, title=title, url=clean_url(url), status=status, notes=_clip(notes, "notes"))
    _stamp(app, status, now)
    db.add(app)
    await db.commit()
    return app


async def track_job(db: AsyncSession, member: Member, job: Job) -> tuple[Application, bool]:
    """Start tracking a job from the site. Tracking the same job twice does nothing. Returns (row, created)."""
    have = await db.scalar(select(Application).where(Application.member_id == member.id, Application.job_id == job.id))
    if have:
        return have, False
    await _room(db, member)
    app = Application(member_id=member.id, job_id=job.id, company=(job.company.name if job.company else job.company_name or "")[:200],
                      title=job.title[:300], url=(job.url or "")[:1000] if (job.url or "").startswith(("http://", "https://", "mailto:")) else "",
                      status="saved")
    db.add(app)
    await db.commit()
    return app, True


async def get_owned(db: AsyncSession, member: Member, app_id: int) -> Application:
    app = await db.get(Application, app_id)
    if not app or app.member_id != member.id:
        raise TrackerError("Not found.", 404)       # the same answer for "does not exist" and "belongs to someone else"
    return app


async def set_status(db: AsyncSession, member: Member, app_id: int, status: str) -> Application:
    app = await get_owned(db, member, app_id)
    app.status = _check_status(status)
    _stamp(app, status, datetime.utcnow())
    await db.commit()
    return app


async def set_notes(db: AsyncSession, member: Member, app_id: int, notes: str) -> Application:
    app = await get_owned(db, member, app_id)
    app.notes = _clip(notes, "notes")
    await db.commit()
    return app


async def delete(db: AsyncSession, member: Member, app_id: int) -> None:
    await db.delete((await get_owned(db, member, app_id)))
    await db.commit()


async def tracked_status(db: AsyncSession, member: Member | None, job_id: int) -> str | None:
    """The status of this job in the member's tracker, or None."""
    if not member:
        return None
    return await db.scalar(select(Application.status).where(Application.member_id == member.id, Application.job_id == job_id))


async def board(db: AsyncSession, member: Member, status: str = "all") -> dict:
    """{'columns': [(status, label, [(application, job_or_None)])], 'counts': {status: n}, 'total': n} for the tracker page."""
    counts = {s: 0 for s in STATUSES}
    for s, n in (await db.execute(select(Application.status, func.count()).where(Application.member_id == member.id).group_by(Application.status))):
        if s in counts:
            counts[s] = n
    q = (select(Application, Job).outerjoin(Job, Job.id == Application.job_id)
         .where(Application.member_id == member.id).order_by(Application.updated_at.desc(), Application.id.desc()))
    if status in STATUSES:
        q = q.where(Application.status == status)
    by_status: dict[str, list] = {s: [] for s in STATUSES}
    for app, job in (await db.execute(q)):
        by_status.setdefault(app.status if app.status in by_status else "saved", []).append((app, job))
    columns = [(s, LABELS[s], by_status[s]) for s in STATUSES if status in ("all", s)]
    return {"columns": columns, "counts": counts, "total": sum(counts.values())}
