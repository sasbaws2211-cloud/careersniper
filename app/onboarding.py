"""
CareerSniper first-time onboarding questionnaire.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Member, MemberProfile


EMPLOYMENT_STATUS = [
    ("employed", "Currently employed"),
    ("unemployed", "Unemployed / between jobs"),
    ("student", "Student / recent graduate"),
    ("freelancer", "Freelancer / contractor"),
    ("other", "Other"),
]

EXPERIENCE_LEVELS = [
    ("entry", "Entry-level / Junior (0–2 years)"),
    ("mid", "Mid-level (2–5 years)"),
    ("senior", "Senior (5–10 years)"),
    ("lead", "Lead / Staff / Principal (10+ years)"),
]

JOB_TYPES = [
    ("full-time", "Full-time"),
    ("contract", "Contract / Freelance"),
    ("part-time", "Part-time"),
    ("internship", "Internship"),
]

REMOTE_PREFS = [
    ("remote", "Remote only"),
    ("hybrid", "Hybrid"),
    ("onsite", "On-site"),
    ("any", "Open to any"),
]

SALARY_RANGES = [
    ("0", "Under $40,000"),
    ("40000", "$40,000 – $60,000"),
    ("60000", "$60,000 – $90,000"),
    ("90000", "$90,000 – $120,000"),
    ("120000", "$120,000 – $160,000"),
    ("160000", "$160,000+"),
    ("", "Prefer not to say"),
]

FRUSTRATIONS = [
    ("no_reply", "Not hearing back after applying"),
    ("low_salary", "Salary offers are too low"),
    ("ghosting", "Companies ghost me after interviews"),
    ("ats", "Applications get filtered out by ATS"),
    ("competition", "Too many applicants for every role"),
    ("stale", "Most listings are stale or already filled"),
    ("other", "Something else"),
]

COMMON_COUNTRIES = [
    "United States", "Canada", "United Kingdom", "Germany", "Netherlands",
    "Ireland", "Australia", "India", "Nigeria", "Ghana", "Kenya",
    "South Africa", "Singapore", "Remote (anywhere)",
]


class OnboardingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def get_profile(db: AsyncSession, member: Member) -> MemberProfile | None:
    return await db.scalar(
        select(MemberProfile).where(MemberProfile.member_id == member.id)
    )


async def is_complete(db: AsyncSession, member: Member) -> bool:
    p = await get_profile(db, member)
    return bool(p and p.completed_at)


async def save_profile(
    db: AsyncSession,
    member: Member,
    data: dict[str, Any],
    *,
    mark_complete: bool = False,
) -> MemberProfile:
    """Create or update the profile from form data."""
    from datetime import datetime

    p = await get_profile(db, member)
    if not p:
        p = MemberProfile(member_id=member.id)
        db.add(p)

    for key in (
        "employment_status", "experience_level", "work_country",
        "remote_preference", "main_frustration",
    ):
        if key in data and data[key] is not None:
            setattr(p, key, str(data[key]).strip()[:60])

    if "desired_titles" in data:
        titles = data["desired_titles"]
        if isinstance(titles, str):
            titles = [t.strip() for t in titles.replace(";", ",").split(",") if t.strip()]
        p.desired_titles = [t[:80] for t in titles][:8]

    if "job_types" in data:
        types = data["job_types"]
        if isinstance(types, str):
            types = [types]
        allowed = {k for k, _ in JOB_TYPES}
        p.job_types = [t for t in types if t in allowed][:4]

    if "salary_range" in data:
        raw = str(data.get("salary_range") or "").strip()
        if raw == "":
            p.salary_min = None
        else:
            try:
                p.salary_min = max(0, int(raw))
            except (ValueError, TypeError):
                p.salary_min = None
    elif "salary_min" in data and data["salary_min"] not in (None, ""):
        try:
            p.salary_min = max(0, int(float(str(data["salary_min"]).replace(",", ""))))
        except (ValueError, TypeError):
            p.salary_min = None

    if "has_resume" in data:
        p.has_resume = str(data["has_resume"]).lower() in ("1", "true", "yes", "on")
    if "tailors_resume" in data:
        p.tailors_resume = str(data["tailors_resume"]).lower() in ("1", "true", "yes", "on")

    p.updated_at = datetime.utcnow()
    if mark_complete:
        p.completed_at = datetime.utcnow()

    await db.commit()
    await db.refresh(p)
    return p


# the questionnaire's levels in the résumé lens' words
_LEVEL_TO_SENIORITY = {"entry": "junior", "mid": "mid", "senior": "senior", "lead": "lead"}


def lens_profile(p: MemberProfile | None) -> dict | None:
    """The job titles and level the member gave, shaped like a résumé profile so the board can filter by it. None without titles."""
    if not p or not (p.desired_titles or []):
        return None
    return {"titles": list(p.desired_titles), "seniority": _LEVEL_TO_SENIORITY.get(p.experience_level or "", "")}


def pitch_for_frustration(frustration: str) -> dict[str, str]:
    pitches = {
        "no_reply": {
            "headline": "Stop applying into the void",
            "body": "CareerSniper shows roles taken straight from company career pages. You apply on the employer's own site — the path that actually gets read.",
        },
        "low_salary": {
            "headline": "See real pay before you apply",
            "body": "We surface salary ranges when companies list them and let you filter by minimum pay, so you waste less time on under-paid roles.",
        },
        "ghosting": {
            "headline": "Apply where decisions actually happen",
            "body": "Jobs on company career sites are managed by the hiring team, not a third-party board that loses your application.",
        },
        "ats": {
            "headline": "Beat the ATS with the right keywords",
            "body": "CareerSniper's AI reads the job description and rewrites your résumé so the keywords the company's ATS is looking for are present.",
        },
        "competition": {
            "headline": "Find openings before the crowd",
            "body": "Many of these roles never appear on LinkedIn or Indeed. You see them while they are still early on the company board.",
        },
        "stale": {
            "headline": "Only roles we recently confirmed open",
            "body": "Every listing shows when we last saw it on the company's own page. Stale postings are removed automatically.",
        },
    }
    return pitches.get(frustration, {
        "headline": "Jobs taken from company career pages",
        "body": "CareerSniper finds openings that often never hit LinkedIn or Indeed, and uses AI to tailor your résumé to each role's keywords.",
    })
