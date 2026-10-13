"""Salary pages: what employers say a role pays, from the pay ranges on the career pages we watch.

This is not a survey. It is the pay that employers published for jobs open now, converted to approximate US dollars a year
(app.classify), using the middle of each posted range. It leans towards employers that publish pay (many tech companies, and places
with pay-transparency laws), and the page says so. To keep a page honest it needs enough data: at least MIN_SAMPLES distinct postings
from at least MIN_COMPANIES employers, so one company posting the same job twenty times cannot set a "market rate".
Interns and trainees are left out (they would drag the middle down), and so are pay figures that cannot be annual salaries.
"""
from __future__ import annotations

import re
import statistics
import time
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import classify, filters
from .models import Job

MIN_SAMPLES = 8
MIN_COMPANIES = 3
MIN_GROUP = 3                 # samples needed to show a seniority or country line
MIN_PAY, MAX_PAY = 8_000, 1_500_000
TTL = 3600
INDEX_LIMIT = 60
_cache: dict = {"at": 0.0, "groups": None, "by_slug": None}

# words that say how senior a job is or where/how it is done, not what it is: "Senior Backend Engineer (Remote)" is a Backend Engineer
_NOISE = {"senior", "sr", "junior", "jr", "lead", "staff", "principal", "associate", "mid", "entry", "level", "ii", "iii", "iv", "remote", "hybrid",
          "onsite", "contract", "contractor", "temporary", "fulltime", "parttime", "full", "part", "time", "m", "f", "d", "w", "x", "all", "genders", "i", "the", "a", "an", "and", "of"}
_SKIP = re.compile(r"\b(intern|interns|internship|trainee|apprentice|apprenticeship|graduate|student|co-?op|volunteer)\b", re.I)
_PARENS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_SUFFIX = re.compile(r"\s[-–—|@]\s.*$|,\s*(remote|hybrid|onsite|us|usa|uk|emea|apac|latam|global)\b.*$", re.I)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:70]


def role_of(title: str) -> tuple[str, str] | None:
    """(key, readable name) for a job title, or None when it should not count (interns) or says too little."""
    t = (title or "").strip()
    if not t or _SKIP.search(t):
        return None
    t = _SUFFIX.sub("", _PARENS.sub(" ", t))
    words = [w for w in re.sub(r"[^a-z0-9+#]+", " ", t.lower()).split() if w not in _NOISE and not w.isdigit()]
    if not words or not any(len(w) >= 3 for w in words) or len(words) > 6:
        return None
    return " ".join(sorted(words)), " ".join(words)


def _wpct(pairs: list[tuple[float, float]], p: float) -> float:
    """The p-th percentile of (value, weight) pairs, interpolating between the points where each value's weight is centred."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    points, run = [], 0.0
    for v, w in pairs:
        points.append(((run + w / 2) / total, v))
        run += w
    if p <= points[0][0]:
        return points[0][1]
    for (a, va), (b, vb) in zip(points, points[1:]):
        if a <= p <= b:
            return va if b == a else va + (vb - va) * (p - a) / (b - a)
    return points[-1][1]


def _summary(rows: list[tuple[float, int]]) -> dict:
    """rows: (pay, company id). Each employer carries the same total weight, so one that posts the same job in twenty countries
    counts once, not twenty times. n is still the number of postings."""
    per_company = Counter(c for _, c in rows)
    pairs = [(v, 1.0 / per_company[c]) for v, c in rows]
    values = [v for v, _ in rows]
    return {"n": len(rows), "median": round(_wpct(pairs, 0.5), -2), "p25": round(_wpct(pairs, 0.25), -2), "p75": round(_wpct(pairs, 0.75), -2),
            "low": round(min(values), -2), "high": round(max(values), -2)}


async def _build(db: AsyncSession) -> tuple[list[dict], dict]:
    from .main import feed_query
    q = feed_query().where(Job.pay_max_usd > 0).with_only_columns(Job.title, Job.pay_min_usd, Job.pay_max_usd, Job.seniority, Job.countries, Job.company_id,
                                                                    Job.dup_key, Job.id, Job.workplace)
    seen: set = set()
    raw: dict[str, list] = defaultdict(list)
    names: dict[str, Counter] = defaultdict(Counter)
    for r in (await db.execute(q)):
        role = role_of(r.title)
        if not role:
            continue
        mid = ((r.pay_min_usd or r.pay_max_usd) + r.pay_max_usd) / 2
        if not MIN_PAY <= mid <= MAX_PAY:
            continue
        ident = (r.company_id, role[0], r.dup_key or f"id:{r.id}")
        if ident in seen:
            continue
        seen.add(ident)
        raw[role[0]].append((mid, r))
        names[role[0]][role[1]] += 1
    groups = []
    for key, rows in raw.items():
        if len(rows) < MIN_SAMPLES or len({r.company_id for _, r in rows}) < MIN_COMPANIES:
            continue
        mids = [(m, r.company_id) for m, r in rows]
        by_level, by_country = defaultdict(list), defaultdict(list)
        for mid, r in rows:
            by_level[r.seniority or "mid"].append((mid, r.company_id))         # no level in the title means mid-level, as in the search filter
            for code in (r.countries or "").strip("|").split("|"):
                if code:
                    by_country[code].append((mid, r.company_id))
        display = names[key].most_common(1)[0][0].title()
        groups.append({"key": key, "name": display, "companies": len({r.company_id for _, r in rows}), **_summary(mids),
                       "remote_pct": round(100 * sum(1 for _, r in rows if r.workplace == "remote") / len(rows)),
                       "levels": [(filters.LEVELS.get(k, k).split(" (")[0], _summary(v)) for k, v in sorted(by_level.items(), key=lambda kv: _summary(kv[1])["median"])
                                  if len(v) >= MIN_GROUP],
                       "countries": [(classify.COUNTRY_NAMES.get(c, c), _summary(v)) for c, v in sorted(by_country.items(), key=lambda kv: -len(kv[1]))
                                     if len(v) >= MIN_GROUP][:5]})
    groups.sort(key=lambda g: (-g["n"], g["name"]))
    by_slug: dict[str, dict] = {}
    for g in groups:
        slug, n = slugify(g["name"]) or "role", 2
        base = slug
        while slug in by_slug:
            slug, n = f"{base}-{n}", n + 1
        g["slug"] = slug
        by_slug[slug] = g
    return groups, by_slug


async def _load(db: AsyncSession) -> tuple[list[dict], dict]:
    if _cache["groups"] is None or time.time() - _cache["at"] > TTL:
        groups, by_slug = await _build(db)
        _cache.update(at=time.time(), groups=groups, by_slug=by_slug)
    return _cache["groups"], _cache["by_slug"]


def clear_cache() -> None:
    _cache.update(at=0.0, groups=None, by_slug=None)


async def index(db: AsyncSession, q: str = "") -> list[dict]:
    groups, _ = await _load(db)
    q = " ".join((q or "").lower().split())
    out = [g for g in groups if q in g["name"].lower()] if q else groups
    return out[:INDEX_LIMIT]


async def get(db: AsyncSession, slug: str) -> dict | None:
    return (await _load(db))[1].get(slug)


async def path_for_title(db: AsyncSession, title: str) -> str | None:
    """The salary page for the role a job title belongs to, if there is one (used for the link on job pages)."""
    role = role_of(title)
    if not role:
        return None
    for g in (await _load(db))[0]:
        if g["key"] == role[0]:
            return f"/salaries/{g['slug']}"
    return None
