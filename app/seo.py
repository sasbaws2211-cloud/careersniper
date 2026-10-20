"""Search and social metadata: titles, descriptions, canonical addresses, robots.txt and the sitemaps.

Rules this module keeps (the page templates call it; they never build these strings themselves):

  * One address per page. The canonical is the public address without query strings, so ?unlocked=1, ?visit= and
    tracking parameters never create copies of a page.
  * Titles are at most 60 characters and end in the brand, so search results do not cut them off.
  * Descriptions are real text from the page (clipped at a word), at most 155 characters, never filler repeated on every page.
  * A page that is private, a search result, or too thin to be useful is `noindex`. A new page with no SEO settings is `noindex`
    by default, so a private page can never leak into search by being forgotten.
  * No job-posting structured data: Google does not allow it for postings whose apply link needs payment (see the README, "SEO").
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

from markupsafe import Markup

from . import config

TITLE_LIMIT = 60
DESCRIPTION_LIMIT = 155
# a job with less description than this is mostly a title and a link: it should not be offered to search engines
MIN_INDEXABLE_DESCRIPTION = 150
# in the sitemap, the same idea measured on the raw HTML (tags and entities make it longer than the readable text)
SITEMAP_MIN_HTML_LENGTH = 300
# the sitemap protocol allows 50,000 addresses per file; stay well under it
SITEMAP_PAGE_SIZE = 40_000

DEFAULT_IMAGE = "/static/og-default.png"
DEFAULT_IMAGE_ALT = "CareerSniper: find jobs that are not on LinkedIn or Indeed"
IMAGE_SIZE = (1200, 630)

# Pages a signed-in person uses, and endpoints that are not pages. Crawlers are told to stay out; the pages are also noindex.
PRIVATE_PATHS = (
    "/account", "/saved", "/tracker", "/resume", "/match", "/letters", "/tailored", "/interview", "/onboarding", "/member",
    "/login", "/logout", "/forgot", "/reset", "/checkout", "/billing", "/alerts", "/following", "/referrals", "/invite",
    "/go/", "/r/", "/api/", "/events", "/webhooks/", "/admin", "/internal", "/health", "/ready",
)
# per-job tools that are only for members
PRIVATE_JOB_SUFFIXES = ("prepare", "tailor", "cover-letter", "coach", "interview", "track", "react", "report-closed")


def absolute(path: str = "/") -> str:
    """The public address for a path on this site. A full address is returned as it is."""
    if path.startswith(("http://", "https://")):
        return path
    return config.PUBLIC_URL + "/" + path.lstrip("/")


_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")


def plain(text: str | None) -> str:
    """Text without tags, entities decoded and whitespace collapsed. A job description is HTML from someone else's page."""
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = _TAG.sub(" ", text)
    return _SPACE.sub(" ", html.unescape(text)).strip()


def clip(text: str | None, limit: int = DESCRIPTION_LIMIT) -> str:
    """At most `limit` characters, cut at a word with an ellipsis, so a description is never cut mid-word."""
    text = plain(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:.-–—")
    return (cut or text[: limit - 1]) + "…"


def brand_title(core: str, brand: str | None = None, limit: int = TITLE_LIMIT) -> str:
    """'<core> | <brand>' within `limit` characters. The core is shortened first; the brand always stays."""
    brand = brand or config.PLATFORM_NAME
    core = plain(core)
    suffix = f" | {brand}"
    room = limit - len(suffix)
    if len(core) > room:
        core = clip(core, max(room, 10))
    return core + suffix


def jsonld(data: object) -> Markup:
    """A JSON-LD block's content. '<', '>' and '&' are escaped so text from a job or a company can never close the script tag."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return Markup(text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def breadcrumbs(items: list[tuple[str, str]]) -> dict:
    """BreadcrumbList structured data from (name, path) pairs, first to last."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": plain(name), "item": absolute(path)} for i, (name, path) in enumerate(items, 1)
        ],
    }


def website_data(description: str) -> list[dict]:
    """The site itself, for the homepage: who it is and what it is called."""
    return [
        {"@context": "https://schema.org", "@type": "WebSite", "name": config.PLATFORM_NAME, "url": absolute("/"), "description": clip(description, 300)},
        {"@context": "https://schema.org", "@type": "Organization", "name": config.PLATFORM_NAME, "url": absolute("/"), "logo": absolute("/static/logo.png")},
    ]


def job_description(job) -> str:
    """The meta description for a job page: the start of the real description, or a plain sentence made from the facts."""
    text = clip(job.description)
    if len(plain(job.description)) >= MIN_INDEXABLE_DESCRIPTION:
        return text
    where = "remote" if getattr(job, "remote", False) and not job.location else (job.location or "")
    place = f" in {where}" if where and where != "remote" else (" (remote)" if where == "remote" else "")
    company = getattr(job, "company_name", "") or (job.company.name if getattr(job, "company", None) else "")
    return clip(f"{job.title} job at {company}{place}. See what it pays, what it asks for and where else it is open.".strip())


def job_indexable(job) -> bool:
    """Offer a job page to search engines only when it has a real description to read."""
    return len(plain(job.description)) >= MIN_INDEXABLE_DESCRIPTION


def job_title(job, brand: str | None = None) -> str:
    """'Backend Engineer at Acme · Berlin | Brand' within 60 characters. When it is too long the place goes first, then the company,
    so the role itself is the last thing to be cut."""
    brand = brand or config.PLATFORM_NAME
    company = getattr(job, "company_name", "") or (job.company.name if getattr(job, "company", None) else "")
    where = job_where(job)
    for core in (f"{job.title} at {company} · {where}" if where else None, f"{job.title} at {company}" if company else None, job.title):
        if core and len(plain(core)) + len(brand) + 3 <= TITLE_LIMIT:
            return f"{plain(core)} | {brand}"
    return brand_title(job.title, brand)


def job_where(job) -> str:
    """'Remote', the location, or both: for a title like 'Backend Engineer at Acme · Remote'."""
    if job.location and getattr(job, "remote", False) and "remote" not in job.location.lower():
        return f"{job.location} · Remote"
    return job.location or ("Remote" if getattr(job, "remote", False) else "")


# ---------------------------------------------------------------- robots.txt and sitemaps
def robots_txt() -> str:
    lines = ["User-agent: *", "Allow: /", ""]
    lines += [f"Disallow: {p}" for p in PRIVATE_PATHS]
    lines += [f"Disallow: /jobs/*/{s}" for s in PRIVATE_JOB_SUFFIXES]
    lines += [
        "",
        "# The job list itself is a page; every filtered, sorted or paged copy of it is not.",
        "Disallow: /jobs?",
        "Disallow: /*?page=",
        "Disallow: /*&page=",
        "",
        f"Sitemap: {absolute('/sitemap.xml')}",
        "",
    ]
    return "\n".join(lines)


def _lastmod(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return value or ""


def urlset(entries: list[tuple[str, datetime | str | None]]) -> str:
    """A sitemap file: (path or address, last change) pairs."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, mod in entries:
        last = _lastmod(mod)
        out.append(f"<url><loc>{xml_escape(absolute(loc))}</loc>" + (f"<lastmod>{last}</lastmod>" if last else "") + "</url>")
    out.append("</urlset>")
    return "\n".join(out)


def sitemap_index(files: list[str]) -> str:
    """The sitemap index: one entry per sitemap file."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    out += [f"<sitemap><loc>{xml_escape(absolute(f))}</loc></sitemap>" for f in files]
    out.append("</sitemapindex>")
    return "\n".join(out)


def job_sitemap_files(job_count: int) -> list[str]:
    """/sitemap-jobs-1.xml ... one per SITEMAP_PAGE_SIZE jobs (at least one, so the index never has a gap)."""
    pages = max(1, -(-job_count // SITEMAP_PAGE_SIZE))
    return [f"/sitemap-jobs-{n}.xml" for n in range(1, pages + 1)]


# The pages with a fixed address that belong in the sitemap. Everything private is left out on purpose.
STATIC_PAGES = ("/", "/jobs", "/pricing", "/companies", "/salaries", "/refunds")
