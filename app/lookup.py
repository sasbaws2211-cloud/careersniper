"""Match a job someone is looking at on another site (LinkedIn, Indeed) to the same role on the employer's own career page.

Used by the browser extension. It receives only a job title, a company name and a location: no account, no history.
Claims are deliberately conservative, because telling someone "this role is on the company's career page" about the wrong
company or the wrong role would be worse than saying nothing:
  * "role"     the company matched exactly (after removing Inc/Ltd and punctuation) and a live job's title matches closely;
  * "similar"  the company matched but no title is a close match: the nearest titles are offered;
  * "company"  the company matched exactly and has open roles, none like this one;
  * "none"     no confident company match.
A company name that only starts like another ("Notion Labs" / "Notion") is accepted only together with a close title match.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import companies, config
from .models import Job

_SUFFIX = {"inc", "incorporated", "llc", "ltd", "limited", "gmbh", "corp", "corporation", "co", "company", "plc", "sa", "ag", "bv", "nv", "pty", "pvt",
           "lp", "llp", "holdings", "group", "the"}
_STOP = {"a", "an", "and", "the", "of", "for", "in", "at", "to", "with", "or", "on", "m", "f", "d", "w", "x"}
ROLE_MIN = 0.75
SIMILAR_MIN = 0.45
MAX_FIELD = 200


def norm_company(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and ")).split()
    while words and words[-1] in _SUFFIX:
        words.pop()
    words = [w for w in words if w != "the"] if len(words) > 1 else words
    return " ".join(words)


def title_tokens(title: str) -> set[str]:
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", (title or "").lower())                  # "(Remote)", "[Contract]"
    t = t.replace("c++", "cpp").replace("c#", "csharp").replace(".net", "dotnet")
    return {w for w in re.sub(r"[^a-z0-9]+", " ", t).split() if w not in _STOP}


def title_score(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


async def _company_row(db: AsyncSession, name: str) -> tuple[dict | None, bool]:
    """(directory row, exact). A prefix match is returned with exact=False."""
    wanted = norm_company(name)
    if not wanted:
        return None, False
    prefix = None
    for row in (await companies._directory_rows(db)):
        n = norm_company(row["name"])
        if n == wanted:
            return row, True
        shorter, longer = sorted((n, wanted), key=len)
        if len(shorter) >= 4 and longer.startswith(shorter + " ") and prefix is None:
            prefix = row
    return prefix, False


def _base() -> str:
    return config.PUBLIC_URL.rstrip("/")


def _job_view(j) -> dict:
    return {"id": j.id, "title": j.title, "location": j.location or "", "url": f"{_base()}/jobs/{j.id}",
            "confirmed_open": j.last_seen.isoformat(timespec="seconds") if j.last_seen else None}


async def find(db: AsyncSession, title: str, company: str, location: str = "") -> dict:
    from .main import feed_query
    title, company, location = (" ".join((x or "").split())[:MAX_FIELD] for x in (title, company, location))
    row, exact = await _company_row(db, company)
    if not row:
        return {"found": "none"}
    cobj = await db.get(companies.Company, row["id"])
    ids = [c.id for c in (await companies.family(db, cobj))] or [row["id"]]
    jobs = (await db.execute(feed_query().where(Job.company_id.in_(ids)).with_only_columns(Job.id, Job.title, Job.location, Job.last_seen, Job.dup_key)
                      .order_by(Job.last_seen.desc(), Job.id.desc()).limit(3000))).all()
    scored = []
    for j in jobs:
        s = title_score(title, j.title)
        if location and j.location and location.lower().split(",")[0].strip() in j.location.lower():
            s += 0.05
        scored.append((min(s, 1.0), j))
    scored.sort(key=lambda t: (-t[0], -t[1].id))
    out = {"company": {"name": row["name"], "url": f"{_base()}{companies.path(cobj)}", "open": row["open"]}}
    if scored and scored[0][0] >= ROLE_MIN:
        out.update(found="role", job=_job_view(scored[0][1]))
        return out
    if not exact:                                    # a company name that only starts alike is never enough on its own
        return {"found": "none"}
    similar, seen = [], set()
    for s, j in scored:
        if s < SIMILAR_MIN or len(similar) == 3:
            break
        key = j.dup_key or f"id:{j.id}"
        if key not in seen:
            seen.add(key)
            similar.append(_job_view(j))
    out.update(found="similar" if similar else "company", similar=similar)
    return out
