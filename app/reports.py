"""'This job is closed' reports from members: the cheapest signal that a listing is dead.

One report per member per job. Enough reports from different confirmed members hide the job from the board (status "closed"); the
scraper never touches `status`, so it stays hidden until an admin sets it back.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Job, JobReport, Member

HIDE_AT = 3          # distinct confirmed members who must agree before the job disappears


async def report_closed(db: AsyncSession, member: Member, job: Job) -> dict:
    """Record the report. Returns {'new': bool, 'count': n, 'hidden': bool}. A second report by the same member is a no-op."""
    have = await db.scalar(select(JobReport.id).where(JobReport.job_id == job.id, JobReport.member_id == member.id))
    if not have:
        db.add(JobReport(job_id=job.id, member_id=member.id))
        await db.commit()
    count = (await db.scalar(
        select(func.count()).select_from(JobReport).join(Member, Member.id == JobReport.member_id)
        .where(JobReport.job_id == job.id, Member.verified.is_(True)))) or 0
    hidden = False
    if count >= HIDE_AT and (job.status in (None, "live")):
        job.status, hidden = "closed", True
        await db.commit()
    return {"new": not have, "count": count, "hidden": hidden or job.status == "closed"}
