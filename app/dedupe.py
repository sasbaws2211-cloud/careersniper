"""Collapse identical postings into one listing.

Employers often publish the same role once per office: same title and same description, different location and
apply link. Two postings are "identical" when they have the same employer, title and (normalised) description.
Nothing is deleted: every posting keeps its own row and apply link; searches show one representative per group
(the newest) with "+N more locations". Postings with a short or missing description are never grouped, since
there is not enough text to be sure they are the same job.
"""
import hashlib
import re
from datetime import datetime

from sqlalchemy import String, bindparam, case, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal, engine
from .models import Job

MIN_DESCRIPTION = 80   # characters of normalised description needed before a posting may be grouped


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip().lower()


def make_key(company_id: int, title: str, description: str | None, external_id: str) -> str:
    desc = _norm(description)
    if len(desc) < MIN_DESCRIPTION:   # not enough text to be sure: unique key, never grouped
        basis = f"unique|{company_id}|{external_id}"
    else:
        basis = f"{company_id}|{_norm(title)}|{desc}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:32]


async def ensure_index() -> None:
    async with engine.begin() as c:
        await c.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_dup_key ON jobs (dup_key)"))


async def backfill(batch: int = 400) -> int:
    """Give every job without a key one (rows from before this feature existed). Idempotent."""
    await ensure_index()
    t = Job.__table__
    done = 0
    async with SessionLocal() as db:
        while True:
            rows = (await db.execute(select(t.c.id, t.c.company_id, t.c.title, t.c.description, t.c.external_id)
                              .where(t.c.dup_key.is_(None)).limit(batch))).all()
            if not rows:
                break
            params, values = {}, []
            for n, r in enumerate(rows):
                params[f"i{n}"], params[f"k{n}"] = r.id, make_key(r.company_id, r.title, r.description, r.external_id)
                values.append(f"SELECT CAST(:i{n} AS INTEGER) AS i, CAST(:k{n} AS VARCHAR) AS k")
            # one UPDATE per batch
            await db.execute(text("UPDATE jobs SET dup_key = v.k FROM (" + " UNION ALL ".join(values) + ") AS v WHERE jobs.id = v.i"), params)
            await db.commit()
            done += len(rows)
    return done


def group_key():
    """Grouping expression; a row somehow without a key is its own group instead of joining all the others."""
    return func.coalesce(Job.dup_key, "id:" + cast(Job.id, String))


async def page(db: AsyncSession, base, rank=None, offset: int = 0, limit: int = 20, sort: str = "new"):
    """(number of distinct listings, rows of the current page) for a filtered `select(Job)` query.
    One representative per group: the most recently posted among those that matched the filters.
    The window runs over a narrow (id, first_seen, rank) projection, not whole rows: job descriptions are
    large, and sorting thousands of them made the first version of this several times slower."""
    featured = case((Job.featured_until > datetime.utcnow(), 1), else_=0)         # a paid/promoted job sorts above the rest
    posted = func.coalesce(Job.posted_at, Job.first_seen)     # the date the card shows: newest-first must mean newest as displayed
    cols = [Job.id.label("jid"), posted.label("seen"), featured.label("feat"),
            func.coalesce(Job.pay_max_usd, 0).label("pay"),      # annual USD; 0 = no pay listed or not convertible, so it sorts last
            func.row_number().over(partition_by=group_key(), order_by=(posted.desc(), Job.first_seen.desc(), Job.id.desc())).label("rn")]
    if rank is not None:
        cols.append(rank.label("rank"))
    sub = base.with_only_columns(*cols).subquery()
    first = sub.c.rn == 1
    total = await db.scalar(select(func.count()).select_from(sub).where(first))
    order = [sub.c.feat.desc()] + ([sub.c.rank.desc()] if rank is not None else []) + [sub.c.seen.desc(), sub.c.jid.desc()]
    if sort == "pay":
        order = [sub.c.pay.desc(), sub.c.seen.desc(), sub.c.jid.desc()]
    ids = list((await db.scalars(select(sub.c.jid).where(first).order_by(*order).offset(offset).limit(limit))))
    by_id = {j.id: j for j in (await db.scalars(select(Job).where(Job.id.in_(ids))))} if ids else {}
    return total, [by_id[i] for i in ids if i in by_id]


async def locations_for(db: AsyncSession, feed, rows: list[Job]) -> dict[str, list[tuple[int, str]]]:
    """{group key: [(job id, location), ...]} for the groups on this page, distinct locations only.
    `feed` is the board's visible-jobs query, so hidden or closed postings never appear."""
    keys = {r.dup_key for r in rows if r.dup_key}
    out: dict[str, list[tuple[int, str]]] = {}
    if not keys:
        return out
    q = feed.where(Job.dup_key.in_(keys)).with_only_columns(Job.dup_key, Job.id, Job.location).order_by(Job.id)
    for key, jid, loc in (await db.execute(q)):
        seen = out.setdefault(key, [])
        if loc and loc not in [l for _, l in seen]:
            seen.append((jid, loc))
    return out
