"""Grow the shared company registry.

Two sources, both public:
  * candidates: probe likely board slugs for a list of company names against each ATS's public
    job-board API. A company is added only if its board answers and has open jobs.
  * Common Crawl: the public CDX index lists career-page URLs; the first path segment of
    boards.greenhouse.io/<token>, jobs.lever.co/<token>, jobs.ashbyhq.com/<token> is the board token.
Run via `python -m app.cli discover candidates <file>` / `discover crawl`.
"""
import html
import json
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Company
from .scrapers import ATS, GUESSABLE, UA, fetch_jobs, scrape_company
from .seed import is_blocked

CRAWL_HOSTS = {
    "greenhouse": ["boards.greenhouse.io", "job-boards.greenhouse.io"],
    "lever": ["jobs.lever.co"],
    "ashby": ["jobs.ashbyhq.com"],
}
JUNK = {"embed", "robots.txt", "jobs", "api", "static", "assets", "favicon.ico", "sitemap.xml", "login", "sign_in",
        "signin", "_next", "v1", "v0", "widget", "careers", "search", "apply", "company", "about"}
URL_RE = re.compile(r'"url":\s*"(https?://[^"]+)"')
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,60}$")
SUFFIXES = ("inc", "llc", "ltd", "corp", "co", "technologies", "labs")


def slug_variants(name: str) -> list[str]:
    """Likely board tokens for a company name: 'Acme Robotics' -> acmerobotics, acme-robotics, acme."""
    words = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    if not words:
        return []
    out = ["".join(words), "-".join(words)]
    if len(words) > 1:
        out.append(words[0])
        if words[-1] in SUFFIXES:
            out.append("".join(words[:-1]))
    seen, res = set(), []
    for v in out:
        if v and v not in seen and TOKEN_RE.match(v):
            seen.add(v)
            res.append(v)
    return res


def tokens_from_urls(urls: list[str], hosts: list[str]) -> set[str]:
    toks = set()
    for u in urls:
        m = re.match(r"https?://([^/]+)/([^/?#]+)", u)
        if not m or m.group(1).lower() not in hosts:
            continue
        t = m.group(2)
        if t.lower() in JUNK or not TOKEN_RE.match(t) or t.isdigit():
            continue
        toks.add(t)
    return toks


def _probe(args) -> tuple | None:
    name, ats, token = args
    try:
        with httpx.Client(timeout=15, headers=UA, follow_redirects=True) as c:
            jobs = fetch_jobs(ats, token, c, light=True)
    except Exception:
        return None
    return (name, ats, token, len(jobs)) if jobs else None


def probe_candidates(names: list[str], workers: int = 12) -> list[tuple]:
    """[(name, ats, token, job_count)] for slug guesses that are live boards with jobs."""
    work = [(n, ats, v) for n in names for v in slug_variants(n) for ats in GUESSABLE]
    with ThreadPoolExecutor(workers) as ex:
        hits = [h for h in ex.map(_probe, work) if h]
    best: dict[str, tuple] = {}
    for h in hits:                      # one board per company name: the one with most jobs
        if h[0] not in best or h[3] > best[h[0]][3]:
            best[h[0]] = h
    return list(best.values())


def crawl_tokens(max_pages: int = 25, crawls: int = 3, log=print) -> dict[str, set[str]]:
    """Board tokens seen in recent Common Crawl indexes. The index endpoint is flaky (504s),
    so every request retries with backoff and failures are skipped, not fatal."""
    c = httpx.Client(timeout=60, headers=UA)

    def get(url, params, tries=4):
        for i in range(tries):
            try:
                r = c.get(url, params=params)
                if r.status_code == 200:
                    return r
            except httpx.HTTPError:
                pass
            time.sleep(2 * (i + 1))
        return None

    info = get("https://index.commoncrawl.org/collinfo.json", {})
    found: dict[str, set[str]] = {k: set() for k in CRAWL_HOSTS}
    if not info:
        log("Common Crawl index unreachable")
        return found
    for coll in info.json()[:crawls]:
        for ats, hosts in CRAWL_HOSTS.items():
            for host in hosts:
                base = {"url": host + "/*", "output": "json", "fl": "url", "filter": "status:200"}
                r = get(coll["cdx-api"], {**base, "showNumPages": "true"})
                try:
                    pages = int(r.json()["pages"]) if r else 0
                except (ValueError, KeyError):
                    pages = 0
                step = max(1, pages // max_pages)   # sample evenly across the alphabet-sorted index
                for p in range(0, pages, step):
                    r = get(coll["cdx-api"], {**base, "page": str(p)})
                    if r:
                        urls = URL_RE.findall(r.text)   # tolerant: the gateway sometimes truncates a line
                        found[ats] |= tokens_from_urls(urls, [host])
                log(f"{coll['id']} {host}: pages={pages} tokens so far={len(found[ats])}")
    return found


def pretty(token: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_.]+", token) if w)


def _greenhouse_name(token: str) -> str | None:
    try:
        r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{token}", timeout=15, headers=UA)
        return r.json().get("name") if r.status_code == 200 else None
    except Exception:
        return None


async def register(db: AsyncSession, found: list[tuple], log=print) -> dict:
    """Add (name, ats, token, _) as shared-registry companies (skipping known tokens), then scrape them."""
    known = {(c.ats, c.board_token.lower()) for c in (await db.scalars(select(Company)))}
    new = []
    for name, ats, token, _n in found:
        if (ats, token.lower()) in known or is_blocked(ats, token):
            continue
        known.add((ats, token.lower()))
        co = Company(name=name[:200], ats=ats, board_token=token)
        db.add(co)
        new.append(co)
    await db.commit()
    jobs = 0
    for i, co in enumerate(new, 1):
        jobs += await scrape_company(db, co)
        if i % 50 == 0:
            log(f"scraped {i}/{len(new)} new companies")
    dead = [c for c in new if c.last_error or c.job_count == 0]
    for c in dead:   # not live after all: don't keep dead boards in the registry
        await db.delete(c)
    await db.commit()
    return {"added": len(new) - len(dead), "dropped": len(dead), "jobs": jobs}


CACHE = Path(__file__).parent.parent / "data" / "discovered.json"
CRAWLED = Path(__file__).parent.parent / "data" / "crawled_tokens.json"


async def discover_from_crawl(db: AsyncSession, log=print, use_cache: bool = True, **kw) -> dict:
    if use_cache and CACHE.exists():   # a previous run already crawled + verified; just (re)register
        named = [tuple(x) for x in json.loads(CACHE.read_text(encoding="utf-8"))]
        log(f"using {len(named)} cached verified boards from {CACHE.name}")
        return await register(db, named, log)
    if use_cache and CRAWLED.exists():
        found = {k: set(v) for k, v in json.loads(CRAWLED.read_text(encoding="utf-8")).items()}
        log("using cached crawl tokens")
    else:
        found = crawl_tokens(log=log, **kw)
        CRAWLED.write_text(json.dumps({k: sorted(v) for k, v in found.items()}), encoding="utf-8")
    known = {(c.ats, c.board_token.lower()) for c in (await db.scalars(select(Company)))}
    cands = [(t, ats) for ats, toks in found.items() for t in sorted(toks) if (ats, t.lower()) not in known]
    log(f"{len(cands)} new tokens to verify")
    verified = []
    with ThreadPoolExecutor(10) as ex:
        for i in range(0, len(cands), 300):   # chunked so progress is visible
            chunk = cands[i:i + 300]
            verified += [h for h in ex.map(_probe, [(t, ats, t) for t, ats in chunk]) if h]
            log(f"verified {min(i + 300, len(cands))}/{len(cands)} tokens, {len(verified)} live boards")
    named = []
    for _, ats, token, n in verified:
        name = (_greenhouse_name(token) if ats == "greenhouse" else None) or pretty(token)
        named.append((name, ats, token, n))
    CACHE.write_text(json.dumps(named), encoding="utf-8")
    return await register(db, named, log)


# ------------------------------------------------------------------ verification of existing boards
def company_from_title(ats: str, title: str) -> str | None:
    """The employer name a job page puts in its <title>: Ashby 'Job @ Company', Lever 'Company - Job'."""
    title = html.unescape(title or "").strip()
    if ats == "ashby":
        return title.rsplit("@", 1)[-1].strip() if "@" in title else None
    if ats == "lever":
        return title.split(" - ", 1)[0].strip() if " - " in title else None
    return None


_SUFFIX = {"inc", "llc", "ltd", "corp", "co", "corporation", "company", "technologies", "technology", "labs", "limited", "gmbh"}


def _words(x: str) -> str:
    ws = re.sub(r"[^a-z0-9 ]", " ", x.lower().replace("&", " and ")).split()
    return "".join(w for w in ws if w not in _SUFFIX)


def match_level(shown: str, stored_name: str, token: str) -> str:
    """'exact' only when the names are the same ignoring case, punctuation, '&' and suffixes like Inc/Labs.
    'partial' (one name inside the other, e.g. 'Collective' vs 'Collective Health') is NOT trusted: those are
    exactly the look-alike cases that turned out to be different companies, so a person must review them."""
    a = _words(shown)
    if not a:
        return "none"
    b = _words(stored_name)   # the token is deliberately NOT used: it is our own guess from the name
    if a == b:
        return "exact"
    return "partial" if (a in b or b in a) else "none"


def _fetch_title(url: str) -> str:
    try:
        h = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}).text
    except Exception:
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I)
    return m.group(1) if m else ""


async def verify_boards(db: AsyncSession, log=print) -> dict:
    """Open two live job pages per Lever/Ashby board and compare the employer name the page shows with the name we
    stored. Greenhouse boards are compared with the board's own `name` field. Returns lists of mismatches / unknowns;
    nothing is changed, so a person decides what to remove (then add it to seed.BLOCKED)."""
    from .models import Job
    cos = list((await db.scalars(select(Company).where(Company.active.is_(True)).order_by(Company.ats, Company.name))))
    work = []
    for c in cos:
        urls = [u for u in (await db.scalars(select(Job.url).where(Job.company_id == c.id, Job.active.is_(True)).limit(2)))]
        work.append((c.id, c.name, c.ats, c.board_token, urls))

    def check(w):
        _id, name, ats, token, urls = w
        if ats == "greenhouse":
            shown = [_greenhouse_name(token) or ""]
        else:
            shown = [company_from_title(ats, _fetch_title(u)) or "" for u in urls]
        shown = [x for x in shown if x]
        if not shown:
            return (name, ats, token, "unknown", [])
        levels = [match_level(x, name, token) for x in shown]
        status = "ok" if "exact" in levels else "review" if "partial" in levels else "mismatch"
        return (name, ats, token, status, shown)

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(check, work))
    out = {k: [r for r in res if r[3] == k] for k in ("ok", "review", "mismatch", "unknown")}
    log(f"{len(out['ok'])} exact match, {len(out['review'])} to review, {len(out['mismatch'])} mismatch, "
        f"{len(out['unknown'])} unknown (of {len(res)})")
    for name, ats, token, _st, shown in out["review"]:
        log(f"  REVIEW   {name} [{ats}/{token}] page says {shown} (similar but not the same name)")
    for name, ats, token, _st, shown in out["mismatch"]:
        log(f"  MISMATCH {name} [{ats}/{token}] page says {shown}")
    for name, ats, token, _st, _ in out["unknown"]:
        log(f"  UNKNOWN  {name} [{ats}/{token}] (no employer name found; check a job page by hand)")
    return out
