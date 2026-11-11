"""The free tools: one list that the Tools menu, the /tools page and the footer all read, so they can never disagree.

A tool that needs the AI is marked `ai`: the pages still work without it, and say so.
"""
from __future__ import annotations

GROUP_ORDER = ("Build", "Check", "Find", "Track")

TOOLS: tuple[dict, ...] = (
    {"key": "resume-builder", "group": "Build", "title": "Résumé builder", "path": "/free-resume-builder",
     "blurb": "Build a résumé with a live preview and download it as PDF, Word or text. No sign-up.", "ai": True},
    {"key": "cv-builder", "group": "Build", "title": "CV builder", "path": "/free-cv-builder",
     "blurb": "Build a CV with education first, plus publications, languages and references.", "ai": True},
    {"key": "resume-job-match", "group": "Check", "title": "Résumé and job match checker", "path": "/tools/resume-job-match",
     "blurb": "Paste a résumé and a job description to see which of the job's keywords you cover and which are missing.", "ai": True},
    {"key": "job-freshness", "group": "Check", "title": "Is this job still open?", "path": "/tools/job-freshness-checker",
     "blurb": "Check how old a posting is, whether it was reposted, and when we last saw it open.", "ai": False},
    {"key": "remote-jobs", "group": "Find", "title": "Remote jobs by country", "path": "/remote-jobs",
     "blurb": "Remote jobs that are really open to people in your country, not just 'remote'.", "ai": False},
    {"key": "employer-page", "group": "Find", "title": "Find the employer's own posting", "path": "/tools/employer-page-finder",
     "blurb": "Saw a job on LinkedIn or Indeed? See if the company has it on its own career page.", "ai": False},
    {"key": "salaries", "group": "Find", "title": "Salaries from real postings", "path": "/salaries",
     "blurb": "What employers say jobs pay, taken from the pay ranges on their own career pages.", "ai": False},
    {"key": "companies", "group": "Find", "title": "Companies hiring", "path": "/companies",
     "blurb": "Companies with open roles right now, and what they are hiring for.", "ai": False},
    {"key": "tracker-sheet", "group": "Track", "title": "Job search tracker spreadsheet", "path": "/tools/job-search-tracker",
     "blurb": "A free Excel tracker with a status list and a live summary. Works in Google Sheets too.", "ai": False},
)


def groups() -> list[tuple[str, list[dict]]]:
    """[(group name, its tools)] in menu order."""
    return [(g, [t for t in TOOLS if t["group"] == g]) for g in GROUP_ORDER]


def paths() -> tuple[str, ...]:
    """Every tool's address, for marking the Tools menu as the current section."""
    return tuple(t["path"] for t in TOOLS) + ("/tools",)


def by_key(key: str) -> dict:
    return next(t for t in TOOLS if t["key"] == key)
