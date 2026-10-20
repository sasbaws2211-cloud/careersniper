"""The blog and the comparison ("alternative") pages: what exists, in what order, and how to find one.

Each entry is a small record here plus a body written as a template fragment under templates/content/<kind>/<slug>.html. Adding a page is
one record and one file; the listing pages, the sitemap, the page's title, description and Article data all come from the record.

Rules for what goes in a comparison page (kept here so they are not forgotten when one is added):
  * Say only what the other product says about itself on its own site, or what could be checked there, and give the date it was checked.
  * Where something could not be confirmed (a price, a limit), say so instead of quoting a third party or guessing.
  * Be fair: say where the other product is the better fit. No reviews quoted, no claims about how a company treats its customers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

BASE = Path(__file__).parent
WORDS_PER_MINUTE = 200


@dataclass(frozen=True)
class Entry:
    kind: str                # "blog" | "alternative"
    slug: str
    title: str               # the page heading (h1) and the Article headline
    seo_title: str           # the title tag before " | CareerSniper": at most 45 characters so the whole title fits in 60
    description: str         # the meta description, at most 155 characters
    summary: str             # one or two sentences for the listing page
    published: date
    updated: date
    competitor: str | None = None

    @property
    def path(self) -> str:
        return f"/{'blog' if self.kind == 'blog' else 'alternatives'}/{self.slug}"

    @property
    def template(self) -> str:
        return f"content/{self.kind}/{self.slug}.html"


BLOG = (
    Entry("blog", "find-jobs-not-on-linkedin-or-indeed", "How to Find Jobs That Are Not on LinkedIn or Indeed", "Find Jobs Not on LinkedIn or Indeed",
          "Many roles are only posted on the company's own career page. Here is how to find them: ATS job boards, targeted searches and a simple routine.",
          "Where the openings that never reach the big job boards live, and a practical way to search them.", date(2026, 10, 20), date(2026, 10, 20)),
    Entry("blog", "what-are-greenhouse-lever-ashby", "Greenhouse, Lever and Ashby: How Company Job Boards Work", "Greenhouse, Lever and Ashby Job Boards",
          "Most company career pages run on an applicant tracking system. Learn what Greenhouse, Lever and Ashby are and how to search the boards they power.",
          "What the hiring systems behind company career pages are, and why a job can be on one and not on LinkedIn.", date(2026, 10, 20), date(2026, 10, 20)),
    Entry("blog", "how-to-spot-a-stale-job-posting", "How to Spot a Stale or Ghost Job Posting", "How to Spot a Stale or Ghost Job Posting",
          "Some job postings are old, filled or never meant to be filled. Seven signs to check before you spend an hour on an application.",
          "Signs that a posting is old, repeated or not really open, and what to do about it.", date(2026, 10, 20), date(2026, 10, 20)),
    Entry("blog", "how-to-read-a-job-posting-pay-range", "How to Read the Pay Range on a Job Posting", "How to Read a Job Posting's Pay Range",
          "A pay range on a posting is not a promise or a guess. What it usually means, where you tend to land in it, and how to use it in negotiation.",
          "What a published pay range tells you, what it does not, and how to use it.", date(2026, 10, 20), date(2026, 10, 20)),
)

ALTERNATIVES = (
    Entry("alternative", "hiringcafe", "HiringCafe Alternative: CareerSniper vs HiringCafe", "HiringCafe Alternative: A Fair Comparison",
          "CareerSniper vs HiringCafe, compared fairly: where each gets its jobs, what each costs, and who should pick which. Checked on 20 October 2026.",
          "Both look for jobs outside LinkedIn and Indeed. How they differ, and when HiringCafe is the better choice.", date(2026, 10, 20), date(2026, 10, 20), "HiringCafe"),
    Entry("alternative", "himalayas", "Himalayas Alternative: CareerSniper vs Himalayas", "Himalayas Alternative: A Fair Comparison",
          "CareerSniper vs Himalayas: a remote-only job board with a free plan against a database of company career pages. Prices checked on 20 October 2026.",
          "A remote job board with free and paid plans compared with a search of company career pages.", date(2026, 10, 20), date(2026, 10, 20), "Himalayas"),
    Entry("alternative", "jobright", "Jobright Alternative: CareerSniper vs Jobright", "Jobright Alternative: A Fair Comparison",
          "CareerSniper vs Jobright: an AI job search copilot against a search of company career pages. What we could and could not confirm on 20 October 2026.",
          "An AI copilot that matches and applies for you, against a search tool for company career pages.", date(2026, 10, 20), date(2026, 10, 20), "Jobright"),
    Entry("alternative", "simplify", "Simplify Alternative: CareerSniper vs Simplify", "Simplify Alternative: A Fair Comparison",
          "CareerSniper vs Simplify: an AI job search platform with tracking and networking against a search of company career pages. Checked on 20 October 2026.",
          "A broad job search platform compared with a focused search of company career pages.", date(2026, 10, 20), date(2026, 10, 20), "Simplify"),
)

KINDS = {"blog": BLOG, "alternative": ALTERNATIVES}


def entries(kind: str) -> list[Entry]:
    """Newest first (by date published, then title), so the listing page and the sitemap are stable."""
    return sorted(KINDS[kind], key=lambda e: (-e.published.toordinal(), e.title))


def get(kind: str, slug: str) -> Entry | None:
    return next((e for e in KINDS[kind] if e.slug == slug), None)


def related(entry: Entry, limit: int = 3) -> list[Entry]:
    """Other pages of the same kind, newest first: for the 'keep reading' list at the end of a page."""
    return [e for e in entries(entry.kind) if e.slug != entry.slug][:limit]


_TAGS = re.compile(r"{%.*?%}|{{.*?}}|<[^>]+>", re.S)


@lru_cache(maxsize=None)
def reading_minutes(kind: str, slug: str) -> int:
    """Minutes to read, from the words in the page's own file (at least 1)."""
    entry = get(kind, slug)
    if entry is None:
        return 1
    text = _TAGS.sub(" ", (BASE / "templates" / entry.template).read_text(encoding="utf-8"))
    return max(1, round(len(text.split()) / WORDS_PER_MINUTE))
