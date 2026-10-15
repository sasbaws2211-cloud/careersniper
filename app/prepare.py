"""'Prepare my application': the four AI steps in the order that works, with what is already done for this job.

Read-only: it looks at what the member has already produced for the job (coach advice, tailored résumé, cover letter, practice
interview, tracker row) and says what is next. It never calls the AI itself.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import coach as coach_mod, letters, resume as resume_mod, tailor, tracker
from .models import InterviewSession, Job, Member

# Informational request estimates for steps; AI enforcement remains on the account's daily allowance.
COSTS = {"tailor": 1, "letter": 1, "interview": 1}


async def steps(db: AsyncSession, member: Member, job: Job) -> dict:
    r = await resume_mod.get_main(db, member)
    has_resume = bool(r and resume_mod.text_of(r))
    coach = coach_mod.cached(r, job.id) if has_resume else None
    doc = await tailor.latest_for_job(db, member, job.id) if has_resume else None
    letter = await letters.latest_for_job(db, member, job.id)
    session = await db.scalar(select(InterviewSession).where(InterviewSession.member_id == member.id, InterviewSession.job_id == job.id)
                              .order_by(InterviewSession.id.desc()))
    tracked = await tracker.tracked_status(db, member, job.id)
    rows = [
        {"key": "resume", "title": "Add your résumé", "done": has_resume, "href": "/resume", "cost": 0,
         "detail": "Everything below is written from it."},
        {"key": "coach", "title": "Check the fit", "done": coach is not None, "href": f"/jobs/{job.id}#coach", "cost": None,
         "detail": coach.get("summary") if coach else "What your résumé shows, what is missing, and the edits worth making."},
        {"key": "tailor", "title": "Tailor your résumé", "done": doc is not None, "href": f"/tailored/{doc.id}" if doc else f"/jobs/{job.id}/tailor",
         "cost": COSTS["tailor"], "detail": "A rewrite for this posting, fact-checked against the original."},
        {"key": "letter", "title": "Write the cover letter", "done": letter is not None, "href": f"/letters/{letter.id}" if letter else f"/jobs/{job.id}/cover-letter",
         "cost": COSTS["letter"], "detail": "Short and specific to this role."},
        {"key": "interview", "title": "Practise the interview", "done": session is not None, "href": f"/interview/{session.id}" if session else None,
         "cost": COSTS["interview"], "detail": "Questions drawn from this posting. Optional, and best done before the call."},
        {"key": "apply", "title": "Apply on the company site", "done": tracked in ("applied", "interviewing", "offer"), "href": f"/go/{job.id}", "cost": 0,
         "detail": "Adds the job to your tracker as applied."},
    ]
    nxt = next((x for x in rows if not x["done"] and x["key"] != "interview"), None)
    return {"rows": rows, "next": nxt["key"] if nxt else None, "done": sum(1 for x in rows if x["done"]), "total": len(rows)}
