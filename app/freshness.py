"""Is this job still open? Finds a posting from a pasted link (or a company and title) and judges it from what we actually know: how long ago it was
posted, whether it was closed and posted again, and when we last saw it open on the company's own career page.

These are signals, not proof. A verdict never says a job is "fake": only that it looks recent, worth checking, old, or gone.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Company, Job

MAX_URL = 600
RECENT_DAYS = 14             # up to this old: recent
AGING_DAYS = 45              # up to this old: ordinary; beyond it, many roles have been filled
OLD_DAYS = 90                # beyond this: very old, possibly a long-running posting
GONE_AFTER_DAYS = 7          # not seen open for this long: probably closed
OPEN_RECENTLY_DAYS = 2

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_LONG_DIGITS = re.compile(r"\d{6,}")
_CODE = re.compile(r"^(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{8,14}$")      # a short code with a digit in it, like a Workable shortcode
# job boards that copy listings: we cannot read them, and say so instead of guessing
BOARD_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com", "simplyhired.com", "jooble.org", "adzuna.com")


def clean_url(raw: str) -> str:
    """The pasted address as a plain https/http address, or '' when it is not one. A bare domain gets https://."""
    u = (raw or "").strip()
    if not u or len(u) > MAX_URL or any(c.isspace() for c in u):
        return ""
    if "://" not in u:
        u = "https://" + u
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        return ""
    return u


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def is_board(url: str) -> bool:
    h = host_of(url)
    return any(h == b or h.endswith("." + b) for b in BOARD_HOSTS)


def tokens(url: str) -> list[str]:
    """Job ids a posting's address may carry: UUIDs, long numbers (a Greenhouse gh_jid, a Workday id) and short codes, from the path and the query.
    Lower case, in order, without repeats."""
    parts = urlsplit(url)
    pieces = [parts.path] + [v for _, v in parse_qsl(parts.query, keep_blank_values=False)]
    found: list[str] = []
    for piece in pieces:
        found += _UUID.findall(piece)
        found += _LONG_DIGITS.findall(piece)
        segments = [] if _UUID.search(piece) else re.split(r"[/_\-]", piece)
        found += [seg for seg in segments if _CODE.match(seg) and not seg.isdigit()]
    out: list[str] = []
    for t in found:
        t = t.lower()
        if t not in out:
            out.append(t)
    return out[:12]


def _norm(url: str) -> str:
    p = urlsplit(url)
    return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}".lower()


def supported(candidate_url: str, board_token: str, wanted_url: str, token: str) -> bool:
    """Whether a job that has `token` as its id is really the one the address points at, not another company's job with the same number.
    A UUID is unique enough on its own; a number or short code also needs the same host, the same address, or the company's board name in the address."""
    if _UUID.fullmatch(token):
        return True
    if host_of(candidate_url) == host_of(wanted_url) or _norm(candidate_url) == _norm(wanted_url):
        return True
    return bool(board_token) and board_token.lower() in wanted_url.lower()


async def find_by_url(db: AsyncSession, url: str) -> Job | None:
    """The tracked job a pasted address points at, or None. Open or closed: a closed one is the point."""
    toks = tokens(url)
    if not toks:
        return None
    rows = (await db.execute(select(Job, Company.board_token).join(Company, Company.id == Job.company_id)
                             .where(func.lower(Job.external_id).in_(toks)).order_by(Job.last_seen.desc()).limit(20))).all()
    wanted = _norm(url)
    chosen = None
    for job, board in rows:
        if not supported(job.url or "", board or "", url, (job.external_id or "").lower()):
            continue
        if _norm(job.url or "") == wanted:
            return job                    # the very address that was pasted
        chosen = chosen or job
    return chosen


def _days(a: datetime | None, now: datetime) -> int | None:
    return None if a is None else max(0, (now - a).days)


def verdict(job, now: datetime | None = None) -> dict:
    """{level, headline, signals, ...} for a job row (anything with active, posted_at, first_seen, last_seen and repost_count).
    level is 'fresh', 'check', 'old' or 'gone'; signals are (tone, text) pairs, tone 'good', 'warn' or 'bad'."""
    now = now or datetime.utcnow()
    posted = getattr(job, "posted_at", None) or getattr(job, "first_seen", None)
    age = _days(posted, now)
    seen = _days(getattr(job, "last_seen", None), now)
    reposts = int(getattr(job, "repost_count", 0) or 0)
    active = bool(getattr(job, "active", False))
    signals: list[tuple[str, str]] = []

    gone = (not active) or (seen is not None and seen > GONE_AFTER_DAYS)
    if gone:
        signals.append(("bad", f"We have not seen it open for {seen} days." if seen is not None else "It is no longer on the company's career page."))
    elif seen is not None and seen <= OPEN_RECENTLY_DAYS:
        signals.append(("good", "It was open on the company's career page when we last checked " + ("today." if seen == 0 else "yesterday." if seen == 1 else f"{seen} days ago.")))
    elif seen is not None:
        signals.append(("good", f"It was open on the company's career page {seen} days ago."))

    level = "gone" if gone else "fresh"
    if age is not None:
        if age <= RECENT_DAYS:
            signals.append(("good", f"Posted {_ago(age)}, which is recent."))
        elif age <= AGING_DAYS:
            signals.append(("good", f"Posted {age} days ago."))
        elif age <= OLD_DAYS:
            signals.append(("warn", f"Posted {age} days ago. Many roles are filled by now, so it is worth checking before you spend time on it."))
            level = level if gone else "check"
        else:
            signals.append(("warn", f"Posted {age} days ago, which is very old. It may be a long-running posting that the company keeps open."))
            level = level if gone else "old"
    if reposts:
        signals.append(("warn", f"It was closed and posted again {_plural(reposts, 'time')}. Some companies do this to keep a role looking new or to collect résumés."))
        if not gone and level == "fresh":
            level = "check"
        if not gone and reposts >= 2:
            level = "old"
    headline = {"fresh": "Looks recent and open", "check": "Worth checking before you apply", "old": "Looks old or long-running", "gone": "Probably closed"}[level]
    return {"level": level, "headline": headline, "signals": signals, "age_days": age, "seen_days": seen, "reposts": reposts, "posted": posted, "active": active}


def _ago(days: int) -> str:
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
