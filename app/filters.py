"""Board filters beyond keywords: country, job type, seniority and 'pay listed'.

The values are derived once per job by app.classify and stored on the row, so filtering is a plain WHERE clause.
A Filters value is also what a saved job alert remembers, in the compact form 'country=US&type=full_time'.
"""
import time
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import classify, regions
from .models import Job

TYPES = dict(classify.JOB_TYPES)
VISA_CHOICES = {"yes": "Sponsors visas", "ok": "Not ruled out (hide \"no sponsorship\")"}
# Choices offered in the min / max pay dropdowns (US dollars per year); any whole number up to MAX_PAY is accepted from a URL
PAY_STEPS = (30_000, 50_000, 75_000, 100_000, 125_000, 150_000, 200_000, 250_000, 300_000)
MAX_PAY = 5_000_000
LEVELS = dict(classify.SENIORITIES) | {"mid": "Mid-level (no level in the title)"}


def _amount(v) -> int:
    """A whole number of dollars from untrusted input, or 0."""
    v = str(v or "").strip().replace(",", "")
    return min(int(v), MAX_PAY) if v.isascii() and v.isdigit() else 0


class Filters(NamedTuple):
    country: str = ""       # ISO alpha-2, e.g. 'US'
    job_type: str = ""      # a key of TYPES
    seniority: str = ""     # a key of LEVELS
    paid: bool = False      # only jobs that state a salary
    pay_min: int = 0        # annual USD: jobs whose pay range reaches at least this
    pay_max: int = 0        # annual USD: jobs whose pay range starts at or below this
    remote_from: str = ""   # a country code or region key (WORLD, EU, APAC ...): remote jobs open to someone there
    visa: str = ""          # 'yes' = the post says it sponsors; 'ok' = it does not say it refuses

    @classmethod
    def make(cls, country: str = "", job_type: str = "", seniority: str = "", paid=False, pay_min="", pay_max="",
             remote_from: str = "", visa: str = "") -> "Filters":
        """Build from untrusted request values: anything unrecognised is dropped, never passed to the query."""
        country = (country or "").strip().upper()
        return cls(country if country in classify.COUNTRY_NAMES else "",
                   job_type if job_type in TYPES else "",
                   seniority if seniority in LEVELS else "",
                   paid in (True, "true", "1", "on", "yes"), _amount(pay_min), _amount(pay_max),
                   (remote_from or "").strip().upper() if (remote_from or "").strip().upper() in regions.VALID_REGIONS else "",
                   visa if visa in VISA_CHOICES else "")

    @classmethod
    def parse(cls, stored: str | None) -> "Filters":
        d = dict(parse_qsl(stored or "", keep_blank_values=False))
        return cls.make(d.get("country", ""), d.get("type", ""), d.get("level", ""), d.get("paid", ""), d.get("pmin", ""), d.get("pmax", ""), d.get("rfrom", ""), d.get("visa", ""))

    @property
    def active(self) -> bool:
        return bool(self.country or self.job_type or self.seniority or self.paid or self.pay_min or self.pay_max or self.remote_from or self.visa)

    def encode(self) -> str:
        parts = {"country": self.country, "type": self.job_type, "level": self.seniority, "paid": "1" if self.paid else "",
                 "pmin": self.pay_min or "", "pmax": self.pay_max or "", "rfrom": self.remote_from, "visa": self.visa}
        return urlencode({k: v for k, v in parts.items() if v})

    def describe(self) -> list[str]:
        out = []
        if self.country:
            out.append(f"in {classify.COUNTRY_NAMES[self.country]}")
        if self.job_type:
            out.append(TYPES[self.job_type].lower())
        if self.seniority:
            out.append(LEVELS[self.seniority].split(" (")[0].lower())
        if self.remote_from:
            out.append("remote, open to " + regions.REGION_NAMES.get(self.remote_from, classify.COUNTRY_NAMES.get(self.remote_from, self.remote_from)))
        if self.visa == "yes":
            out.append("visa sponsorship offered")
        elif self.visa == "ok":
            out.append("visa sponsorship not ruled out")
        if self.pay_min and self.pay_max:
            out.append(f"pay ${self.pay_min:,}–${self.pay_max:,} a year")
        elif self.pay_min:
            out.append(f"pay of ${self.pay_min:,}+ a year")
        elif self.pay_max:
            out.append(f"pay up to ${self.pay_max:,} a year")
        elif self.paid:
            out.append("pay listed")
        return out

    def apply(self, q):
        if self.country:
            q = q.where(Job.countries.contains(f"|{self.country}|"))
        if self.job_type == "full_time":     # most postings never say; treating "not stated" as full-time is the honest default
            q = q.where(or_(Job.job_type == "full_time", Job.job_type == "", Job.job_type.is_(None)))
        elif self.job_type:
            q = q.where(Job.job_type == self.job_type)
        if self.seniority == "mid":
            q = q.where(or_(Job.seniority == "", Job.seniority.is_(None)))
        elif self.seniority:
            q = q.where(Job.seniority == self.seniority)
        if self.paid:
            q = q.where(or_(Job.salary_min.isnot(None), Job.salary_max.isnot(None)))
        if self.remote_from:     # a remote job whose location says where it can be done from: that place, or a wider one, or anywhere
            q = q.where(Job.workplace == "remote",
                        or_(*[Job.remote_regions.contains(f"|{t}|") for t in sorted(regions.match_tokens(self.remote_from))]))
        if self.visa == "yes":
            q = q.where(Job.visa == "yes")
        elif self.visa == "ok":
            q = q.where(or_(Job.visa.is_(None), Job.visa != "no"))
        if self.pay_min or self.pay_max:      # needs pay we could convert to dollars a year (0 marks pay we could not)
            q = q.where(Job.pay_max_usd > 0)
            if self.pay_min:
                q = q.where(Job.pay_max_usd >= self.pay_min)
            if self.pay_max:
                q = q.where(Job.pay_min_usd <= self.pay_max)
        return q


NO_FILTERS = Filters()

# ---------------------------------------------------------------- country dropdown
_CACHE: dict = {}
CACHE_SECONDS = 120


def clear_cache() -> None:
    _CACHE.clear()


async def country_options(db: AsyncSession, feed, key) -> list[tuple[str, str]]:
    """[(code, name)] for the countries that have at least one job in this board's feed, most jobs first.
    `feed` is the board's base query (before any country filter); `key` identifies it for the short-lived cache."""
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    sub = feed.with_only_columns(Job.countries).subquery()
    counts: dict[str, int] = {}
    for stored, n in (await db.execute(select(sub.c.countries, func.count()).group_by(sub.c.countries))):
        for code in (stored or "").strip("|").split("|"):
            if code:
                counts[code] = counts.get(code, 0) + n
    opts = [(c, classify.COUNTRY_NAMES.get(c, c)) for c, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    _CACHE[key] = (time.monotonic(), opts)
    return opts


async def remote_region_options(db: AsyncSession, feed, key) -> list[tuple[str, str]]:
    """[(key, label)] for the 'Remote from' dropdown: the regions and countries that this board's remote jobs are open to."""
    hit = _CACHE.get(("remote",) + tuple(key))
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    sub = feed.where(Job.workplace == "remote", Job.remote_regions != "").with_only_columns(Job.remote_regions).subquery()
    counts: dict[str, int] = {}
    for stored, n in (await db.execute(select(sub.c.remote_regions, func.count()).group_by(sub.c.remote_regions))):
        for token in (stored or "").strip("|").split("|"):
            if token:
                counts[token] = counts.get(token, 0) + n
    opts = regions.region_options(counts)
    _CACHE[("remote",) + tuple(key)] = (time.monotonic(), opts)
    return opts


# ---------------------------------------------------------------- filling the derived columns
async def backfill(db: AsyncSession, batch: int = 1000, everything: bool = False) -> int:
    """Classify jobs that have no derived values yet (rows from before filters existed). everything=True re-runs all
    of them, e.g. after the classifier has been improved. Idempotent: classified rows hold '' (or 0 for pay that cannot
    be converted) rather than NULL, so nothing is picked up twice."""
    has_pay = or_(Job.salary_min.isnot(None), Job.salary_max.isnot(None))
    done, last_id = 0, 0
    while True:
        q = (select(Job.id, Job.title, Job.location, Job.employment_type, Job.salary_min, Job.salary_max, Job.salary_currency,
                    Job.salary_period, Job.description, Job.remote, Job.workplace).where(Job.id > last_id).order_by(Job.id).limit(batch))
        if not everything:
            q = q.where(or_(Job.seniority.is_(None), Job.workplace.is_(None), and_(has_pay, Job.pay_max_usd.is_(None))))
        rows = (await db.execute(q)).all()
        if not rows:
            return done
        await db.execute(update(Job), [dict(id=r.id, **classify.classify(dict(
            title=r.title, location=r.location or "", employment_type=r.employment_type or "", salary_min=r.salary_min,
            salary_max=r.salary_max, salary_currency=r.salary_currency, salary_period=r.salary_period, description=r.description,
            remote=r.remote, workplace=r.workplace))) for r in rows])
        await db.commit()
        done += len(rows)
        last_id = rows[-1].id
