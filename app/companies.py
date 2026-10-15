"""Company pages: who is hiring, how much, and how fast. All of it comes from what the scraper has watched, nothing is estimated.

What the numbers mean (and the page says so):
  * the first time we scrape a company, every job on its board looks "new". Those are jobs that were already open, not hiring we
    saw happen, so jobs stamped within minutes of our first look at that board are left out of "opened" and of the hiring pace;
  * a job is "closed" when it vanished from the company's board; the date is the last time we saw it;
  * the trend needs two weeks of watching before it is shown, the pace needs five jobs we saw open and close.
Two board records with the same company name (for example the same employer on Greenhouse and on Lever) are one company here.
"""
from __future__ import annotations

import re
import statistics
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import cache, classify, dedupe
from .models import Company, Job

BASELINE = timedelta(minutes=10)     # one scrape stamps every job on a board with the same moment; later than this is a job we saw appear
WEEKS = 12
MIN_TRACKING_DAYS = 14
MIN_PACE_SAMPLES = 5
PER_PAGE = 20
DIRECTORY_PER_PAGE = 30
DIRECTORY_TTL = 120
_cache: dict = {"at": 0.0, "rows": None}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60] or "company"


def path(company: Company) -> str:
    return f"/companies/{company.id}-{slugify(company.name)}"


async def family(db: AsyncSession, company: Company) -> list[Company]:
    """Every active board record with this company's name (case-insensitive), lowest id first."""
    return list((await db.scalars(select(Company).where(func.lower(Company.name) == company.name.lower(), Company.active.is_(True)).order_by(Company.id))))


async def canonical(db: AsyncSession, company: Company) -> Company:
    fam = await family(db, company)
    return fam[0] if fam else company


def parse_id(slug_id: str) -> int | None:
    m = re.match(r"(\d{1,9})(?:-|$)", slug_id or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- the directory
async def _directory_rows(db: AsyncSession) -> list[dict]:
    from .main import feed_query                   # lazy: main imports this module
    if _cache["rows"] is not None and time.time() - _cache["at"] < DIRECTORY_TTL:
        return _cache["rows"]
    shared = await cache.get_json("public:company-directory:v1")
    if isinstance(shared, list) and all(
        isinstance(row, dict) and isinstance(row.get("id"), int) and isinstance(row.get("name"), str)
        and isinstance(row.get("slug"), str) and isinstance(row.get("open"), int)
        for row in shared
    ):
        _cache.update(at=time.time(), rows=shared)
        return shared
    counts = dict((await db.execute(feed_query().with_only_columns(Job.company_id, func.count(func.distinct(dedupe.group_key()))).group_by(Job.company_id))).all())
    by_name: dict[str, dict] = {}
    for c in (await db.scalars(select(Company).where(Company.active.is_(True)).order_by(Company.id))):
        n = counts.get(c.id, 0)
        if not n:
            continue
        row = by_name.setdefault(c.name.lower(), {"id": c.id, "name": c.name, "slug": slugify(c.name), "open": 0})
        row["open"] += n
    rows = sorted(by_name.values(), key=lambda r: (-r["open"], r["name"].lower()))
    _cache.update(at=time.time(), rows=rows)
    await cache.set_json("public:company-directory:v1", rows, DIRECTORY_TTL)
    return rows


def clear_cache() -> None:
    _cache.update(at=0.0, rows=None)


async def lens_counts(db: AsyncSession, lens) -> dict[str, int]:
    """{lower-cased company name: open roles that fit the member's résumé}."""
    from .main import feed_query                   # lazy: main imports this module
    q = lens.apply(feed_query()).with_only_columns(func.lower(Company.name), func.count(func.distinct(dedupe.group_key()))).group_by(func.lower(Company.name))
    return {name: n for name, n in (await db.execute(q)).all()}


async def directory(db: AsyncSession, q: str = "", page: int = 1, lens=None) -> tuple[int, list[dict]]:
    rows = await _directory_rows(db)
    if lens is not None:
        counts = await lens_counts(db, lens)
        rows = sorted(({**r, "match": counts[r["name"].lower()]} for r in rows if counts.get(r["name"].lower())),
                      key=lambda r: (-r["match"], -r["open"], r["name"].lower()))
    q = " ".join((q or "").lower().split())
    if q:
        rows = [r for r in rows if q in r["name"].lower()]
    page = max(1, page)
    return len(rows), rows[(page - 1) * DIRECTORY_PER_PAGE: page * DIRECTORY_PER_PAGE]


# ---------------------------------------------------------------- one company
async def stats(db: AsyncSession, ids: list[int], now: datetime | None = None) -> dict:
    from .main import feed_query
    now = now or datetime.utcnow()
    base = feed_query().where(Job.company_id.in_(ids))
    open_total = (await db.scalar(select(func.count()).select_from(
        base.with_only_columns(func.min(Job.id)).group_by(dedupe.group_key()).subquery()))) or 0

    rows = (await db.execute(select(Job.company_id, Job.first_seen, Job.last_seen, Job.active).where(Job.company_id.in_(ids)))).all()
    looks = await first_looks(db, ids)
    first_look = min(looks.values(), default=None)
    tracking_days = (now - first_look).days if first_look else 0
    observed = [r for r in rows if is_new(r.first_seen, looks.get(r.company_id))]      # jobs we saw appear, not jobs that were already there

    opened, closed = [0] * WEEKS, [0] * WEEKS
    for r in observed:
        i = (now - r.first_seen).days // 7
        if 0 <= i < WEEKS:
            opened[i] += 1
    for r in rows:
        if not r.active and r.last_seen:
            i = (now - r.last_seen).days // 7
            if 0 <= i < WEEKS:
                closed[i] += 1
    trend = []
    for i in range(WEEKS - 1, -1, -1):                      # oldest week first
        start = now - timedelta(days=7 * (i + 1))
        trend.append({"label": start.strftime("%d %b"), "opened": opened[i], "closed": closed[i]})

    lifetimes = [(r.last_seen - r.first_seen).total_seconds() / 86400 for r in observed if not r.active and r.last_seen and r.last_seen >= r.first_seen]
    pace = round(statistics.median(lifetimes)) if len(lifetimes) >= MIN_PACE_SAMPLES else None

    live = (await db.execute(base.with_only_columns(Job.dup_key, Job.id, Job.workplace, Job.remote, Job.countries))).all()
    seen, remote, countries = set(), 0, {}
    for r in live:
        key = r.dup_key or f"id:{r.id}"
        if key in seen:
            continue
        seen.add(key)
        remote += 1 if (r.workplace == "remote" or (r.remote and not r.workplace)) else 0
        for code in (r.countries or "").strip("|").split("|"):
            if code:
                countries[code] = countries.get(code, 0) + 1
    top = [(classify.COUNTRY_NAMES.get(c, c), n) for c, n in sorted(countries.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    return {"open": open_total, "new_30d": sum(1 for r in observed if (now - r.first_seen).days < 30), "tracking_days": tracking_days,
            "tracking_since": first_look, "trend_ok": tracking_days >= MIN_TRACKING_DAYS, "trend": trend,
            "trend_max": max([1] + [t["opened"] for t in trend] + [t["closed"] for t in trend]),
            "opened_12w": sum(opened), "closed_12w": sum(closed), "pace_days": pace, "pace_samples": len(lifetimes),
            "remote_pct": round(100 * remote / len(seen)) if seen else 0, "top_countries": top}


async def first_looks(db: AsyncSession, ids: list[int]) -> dict[int, datetime]:
    """When each board record was first scraped (the earliest first_seen of any of its jobs)."""
    return {cid: t for cid, t in (await db.execute(select(Job.company_id, func.min(Job.first_seen)).where(Job.company_id.in_(ids)).group_by(Job.company_id))) if t}


def is_new(first_seen: datetime | None, first_look: datetime | None) -> bool:
    """True for a job we saw appear, False for one that was already on the board when we first looked."""
    return bool(first_seen and first_look and first_seen > first_look + BASELINE)


async def jobs_page(db: AsyncSession, ids: list[int], page: int, per: int = PER_PAGE) -> tuple[int, list[Job]]:
    from .main import feed_query
    return await dedupe.page(db, feed_query().where(Job.company_id.in_(ids)), None, (max(1, page) - 1) * per, per)
