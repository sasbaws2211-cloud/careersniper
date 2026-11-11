"""Remote jobs by country: which places have a page, what each page's address is, and the numbers on it.

A remote job is "open to" a place when its location names that place, a region group the place is in, or the whole world (`regions.match_tokens`).
A post that only says "Remote" names no place and is matched to none: the pages say so instead of pretending.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import classify, dedupe, regions
from .filters import Filters
from .models import Company, Job

MIN_INDEXED = 5          # a place with fewer remote jobs has a page, but it is kept out of the sitemap and told not to be indexed
CACHE_SECONDS = 600
WORLD = "WORLD"
# the countries people ask about most; they are always candidates, even when no job names them (a worldwide job is open to them too)
ALWAYS = ("US", "GB", "CA", "AU", "DE", "NL", "IE", "FR", "ES", "IN", "PK", "BD", "PH", "SG", "NG", "GH", "KE", "ZA", "EG", "BR", "MX", "CO", "AR")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _build() -> tuple[dict[str, str], dict[str, str]]:
    by_code: dict[str, str] = {WORLD: "worldwide"}
    for code, name in regions.REGION_NAMES.items():
        by_code.setdefault(code, _slugify(name))
    for code, name in classify.COUNTRY_NAMES.items():
        by_code.setdefault(code, _slugify(name))
    by_slug: dict[str, str] = {}
    for code, slug in by_code.items():
        by_slug.setdefault(slug, code)
    return by_code, by_slug


_SLUG_OF, _CODE_OF = _build()


def slug_of(code: str) -> str:
    return _SLUG_OF.get(code, "")


def code_for(slug: str) -> str:
    """The country or region code for a page address, or '' when there is no such page."""
    return _CODE_OF.get((slug or "").strip().lower(), "")


def path_of(code: str) -> str:
    return f"/remote-jobs/{slug_of(code)}"


def is_region(code: str) -> bool:
    return code in regions.REGION_NAMES


def name_of(code: str) -> str:
    return regions.REGION_NAMES.get(code) or classify.COUNTRY_NAMES.get(code, code)


def heading(code: str) -> str:
    """'Remote jobs open to people in Ghana', 'Remote jobs open to Europe', 'Worldwide remote jobs'."""
    if code == WORLD:
        return "Worldwide remote jobs"
    return f"Remote jobs open to {'people in ' if not is_region(code) else ''}{name_of(code)}"


def place(code: str) -> str:
    """The place as it reads inside a sentence."""
    return "anywhere in the world" if code == WORLD else name_of(code)


def observed_tokens(region_strings) -> set[str]:
    """Every place named by the stored `remote_regions` values ('|CA|US|' ...)."""
    out: set[str] = set()
    for s in region_strings:
        out |= {t for t in (s or "").strip("|").split("|") if t}
    return out


def candidates(tokens: set[str]) -> list[str]:
    """The places worth counting: the world and every group, plus the countries named by a job or asked about most, when a job could be open to them."""
    codes = {WORLD} | set(regions.GROUPS) | (set(tokens) & set(classify.COUNTRY_NAMES)) | set(ALWAYS)
    return sorted(c for c in codes if c in regions.VALID_REGIONS and regions.match_tokens(c) & tokens)


def order(counts: dict[str, int]) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """(regions, countries) as (code, count), each most jobs first, leaving out places below the indexing threshold."""
    keep = [(c, n) for c, n in counts.items() if n >= MIN_INDEXED]
    regs = sorted((kv for kv in keep if is_region(kv[0])), key=lambda kv: (regions.GROUP_ORDER.index(kv[0]) if kv[0] in regions.GROUP_ORDER else 99, kv[0]))
    ctry = sorted((kv for kv in keep if not is_region(kv[0])), key=lambda kv: (-kv[1], name_of(kv[0])))
    return regs, ctry


_cache: dict = {}


def clear_cache() -> None:
    _cache.clear()


def _distinct():
    return func.count(func.distinct(dedupe.group_key()))


async def counts(db: AsyncSession, feed, key) -> dict[str, int]:
    """{place code: number of distinct remote listings open to it}, counted the way the listing counts them. Kept for ten minutes."""
    hit = _cache.get(("counts", key))
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    remote = feed.where(Job.workplace == "remote", Job.remote_regions != "")
    sub = remote.with_only_columns(Job.remote_regions).distinct().subquery()
    tokens = observed_tokens(r[0] for r in (await db.execute(select(sub.c.remote_regions))).all())
    out: dict[str, int] = {}
    for code in candidates(tokens):
        n = (await db.scalar(Filters.make(remote_from=code).apply(feed).with_only_columns(_distinct()))) or 0
        if n:
            out[code] = n
    _cache[("counts", key)] = (time.monotonic(), out)
    return out


async def facts(db: AsyncSession, feed, code: str) -> dict:
    """The numbers for one place's page: companies hiring, how many state pay, the levels, visa sponsorship and when we last saw one open."""
    base = Filters.make(remote_from=code).apply(feed)
    total = (await db.scalar(base.with_only_columns(_distinct()))) or 0
    top = (await db.execute(base.with_only_columns(Company.name, _distinct()).group_by(Company.name).order_by(_distinct().desc(), Company.name).limit(8))).all()
    paid = (await db.scalar(base.where(Job.pay_max_usd > 0).with_only_columns(_distinct()))) or 0
    visa = (await db.scalar(base.where(Job.visa == "yes").with_only_columns(_distinct()))) or 0
    levels = (await db.execute(base.with_only_columns(Job.seniority, _distinct()).group_by(Job.seniority))).all()
    newest = await db.scalar(base.with_only_columns(func.max(Job.last_seen)))
    companies = (await db.scalar(base.with_only_columns(func.count(func.distinct(Company.id))))) or 0
    token = f"|{code}|"
    if code == WORLD:
        direct, world = 0, total
    elif is_region(code):
        direct, world = 0, (await db.scalar(base.where(Job.remote_regions.contains("|WORLD|")).with_only_columns(_distinct()))) or 0
    else:
        direct = (await db.scalar(base.where(Job.remote_regions.contains(token)).with_only_columns(_distinct()))) or 0
        world = (await db.scalar(base.where(Job.remote_regions.contains("|WORLD|"), ~Job.remote_regions.contains(token)).with_only_columns(_distinct()))) or 0
    regional = max(0, total - direct - world)             # open to a wider region that includes the place
    names = {k: v for k, v in classify.SENIORITIES}
    named = sorted(((names.get(s or "", ""), n) for s, n in levels if s), key=lambda kv: -kv[1])
    return {"total": total, "companies": companies, "top_companies": [(n, c) for n, c in top], "paid": paid, "paid_pct": round(100 * paid / total) if total else 0,
            "visa": visa, "direct": direct, "world": world, "regional": regional, "levels": [(n.split(" (")[0], c) for n, c in named if n][:3], "newest": newest}


def phrase_levels(levels: list[tuple[str, int]]) -> str:
    return ", ".join(f"{n.lower()} ({c})" for n, c in levels)


def ago(when: datetime | None, now: datetime | None = None) -> str:
    """'today', 'yesterday' or 'N days ago' for a time we last saw a job open."""
    if not when:
        return "recently"
    days = max(0, ((now or datetime.utcnow()) - when).days)
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
