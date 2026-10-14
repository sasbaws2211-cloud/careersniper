import asyncio
"""Fetch jobs from the public JSON endpoints that company career pages already use.

No login, browser or anti-bot handling: Greenhouse, Lever and Ashby each expose a
documented public job-board API. Parsers are pure functions so they are easy to test.
"""
import hashlib
import html
import re
import time
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import classify, config
from . import dedupe
from .models import Company, Job

TIMEOUT = 25
RETRY_AFTER = timedelta(minutes=30)   # re-try boards whose last refresh errored
UA = {"User-Agent": "CareerSniper/1.0 (+job aggregator; public ATS APIs)"}
_TAG = re.compile(r"<[^>]+>")


def _demojibake(s: str) -> str:
    """Some boards (Workday) serve UTF-8 text that was decoded as cp1252 once too often: 'We’re' arrives as 'Weâ€™re'.
    Undo it only when the round trip is clean, so genuine accented text is never touched."""
    if "â" in s or "Ã" in s:
        try:
            return s.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
    return s


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", _demojibake(html.unescape(s or "")))).strip()


_BLOCK_END = re.compile(r"</(?:p|div|section|article|ul|ol|table|tr|blockquote)\s*>|<br\s*/?>|<hr\s*/?>", re.I)
_HEAD_END = re.compile(r"</h[1-6]\s*>", re.I)
_LI_START = re.compile(r"<li[^>]*>", re.I)
_LI_END = re.compile(r"</li\s*>", re.I)


def _desc(s: str) -> str:
    """A job description as plain text that keeps its structure: one line per paragraph, heading and bullet (the page renders it with
    app/textfmt.py). `_text` flattens everything onto one line, which is right for short fields but not for a posting."""
    t = _demojibake(html.unescape(s or ""))
    t = _HEAD_END.sub("\n\n", t)
    t = _LI_END.sub("", _LI_START.sub("\n• ", t))
    t = _BLOCK_END.sub("\n\n", t)
    t = _TAG.sub(" ", t)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in t.replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _remote(*bits) -> bool:
    return any("remote" in (b or "").lower() for b in bits)


def _dt(v):
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).replace(tzinfo=None)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" UTC", "+00:00"))
        return d if d.tzinfo is None else d.astimezone(timezone.utc).replace(tzinfo=None)   # date-only values are already UTC
    except ValueError:
        return None


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _greenhouse_pay(j: dict) -> dict:
    """The first published pay range of a job. Greenhouse gives cents and a free-text title ('Annual Pay Range',
    'Hourly Rate:', 'Mexico Monthly Pay Range') that usually says the period; jobs can carry one range per region."""
    for r in j.get("pay_input_ranges") or []:
        lo, hi = r.get("min_cents"), r.get("max_cents")
        if lo or hi:
            return dict(salary_min=int(lo / 100) if lo else None, salary_max=int(hi / 100) if hi else None,
                        salary_currency=(r.get("currency_type") or "")[:8], salary_period=period_of_title(r.get("title")))
    return {}


def period_of_title(title: str | None) -> str:
    return classify.period_of(title)


def parse_greenhouse(data: dict) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        dept = ", ".join(d.get("name", "") for d in j.get("departments", []) if d)
        out.append(dict(external_id=str(j["id"]), title=j.get("title", ""), location=loc,
                        remote=_remote(loc, j.get("title")), department=dept,
                        url=j.get("absolute_url", ""), description=_desc(j.get("content", ""))[:8000],
                        posted_at=_dt(j.get("first_published") or j.get("updated_at")), **_greenhouse_pay(j)))
    return out


def parse_lever(data: list) -> list[dict]:
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        sal = j.get("salaryRange") or {}
        loc = cat.get("location") or ", ".join(cat.get("allLocations") or [])
        out.append(dict(external_id=str(j["id"]), title=j.get("text", ""), location=loc or "",
                        remote=(j.get("workplaceType") == "remote") or _remote(loc), workplace=j.get("workplaceType"),
                        department=cat.get("team") or cat.get("department") or "",
                        employment_type=cat.get("commitment") or "", url=j.get("hostedUrl", ""),
                        description=(j.get("descriptionPlain") or _desc(j.get("description", "")))[:8000],
                        salary_min=sal.get("min"), salary_max=sal.get("max"),
                        salary_currency=sal.get("currency") or "", salary_period=classify.period_of(sal.get("interval")),
                        posted_at=_dt(j.get("createdAt"))))
    return out


def parse_ashby(data: dict) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {})
        summ = (comp.get("summaryComponents") or [])
        base = next((c for c in summ if c.get("compensationType") == "Salary"), {})
        out.append(dict(external_id=str(j["id"]), title=j.get("title", ""), location=j.get("location", "") or "",
                        # isRemote is also true for hybrid roles, so it only counts when the posting gives no workplaceType
                        remote=(bool(j.get("isRemote")) and not j.get("workplaceType")) or _remote(j.get("location")),
                        workplace=j.get("workplaceType"),
                        department=j.get("department") or j.get("team") or "",
                        employment_type=j.get("employmentType") or "", url=j.get("jobUrl", ""),
                        description=(j.get("descriptionPlain") or _desc(j.get("descriptionHtml", "")))[:8000],
                        salary_min=base.get("minValue"), salary_max=base.get("maxValue"),
                        salary_currency=base.get("currencyCode") or "", salary_period=classify.period_of(base.get("interval")),
                        posted_at=_dt(j.get("publishedAt"))))
    return out


def parse_workable(data: dict) -> list[dict]:
    out = []
    for j in data.get("jobs", []) if isinstance(data, dict) else []:
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        out.append(dict(external_id=str(j.get("shortcode") or j.get("id") or ""), title=j.get("title", ""), location=loc,
                        remote=bool(j.get("telecommuting")) or _remote(loc, j.get("title")),
                        department=j.get("department") or "", employment_type=j.get("employment_type") or "",
                        url=j.get("url") or j.get("shortlink") or "", description=_desc(j.get("description", ""))[:8000],
                        posted_at=_dt(j.get("published_on") or j.get("created_at"))))
    return [j for j in out if j["external_id"]]


def parse_recruitee(data: dict) -> list[dict]:
    out = []
    for j in data.get("offers", []) if isinstance(data, dict) else []:
        if j.get("status") and j["status"] != "published":
            continue
        loc = j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        sal = j.get("salary") if isinstance(j.get("salary"), dict) else {}
        out.append(dict(external_id=str(j.get("id") or j.get("guid") or ""), title=j.get("title", ""), location=loc,
                        remote=bool(j.get("remote")) or _remote(loc, j.get("title")),
                        department=j.get("department") or "",
                        employment_type=(j.get("employment_type_code") or "").replace("_", " "),
                        url=j.get("careers_url", ""),
                        description=_desc((j.get("description") or "") + "<br><br>" + (j.get("requirements") or ""))[:8000],
                        salary_min=_int(sal.get("min")), salary_max=_int(sal.get("max")), salary_currency=sal.get("currency") or "",
                        posted_at=_dt(j.get("published_at") or j.get("created_at"))))
    return [j for j in out if j["external_id"]]


def parse_smartrecruiters(data: dict) -> list[dict]:
    """The list endpoint carries no description; scrape_company fills it in per posting (see sr_description)."""
    out = []
    for j in data.get("content", []) if isinstance(data, dict) else []:
        loc = j.get("location") or {}
        full = ", ".join(p.strip() for p in (loc.get("fullLocation") or "").split(",") if p.strip())
        company = (j.get("company") or {}).get("identifier") or ""
        out.append(dict(external_id=str(j.get("id", "")), title=(j.get("name") or "").strip(), location=full,
                        remote=bool(loc.get("remote")) or _remote(full),
                        workplace="remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else None),
                        department=(j.get("department") or {}).get("label", "") or "",
                        employment_type=(j.get("typeOfEmployment") or {}).get("label", "") or "",
                        url=f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}" if company and j.get("id") else "",
                        description="", posted_at=_dt(j.get("releasedDate"))))
    return [j for j in out if j["external_id"]]


def parse_bamboohr(data: dict) -> list[dict]:
    """The list endpoint has no text or date; scrape_company fills both from the per-job detail (bamboo_detail)."""
    token, out = data.get("_token", ""), []
    for j in data.get("result", []) if isinstance(data, dict) else []:
        ats_loc, loc = j.get("atsLocation") or {}, j.get("location") or {}
        city = ats_loc.get("city") or loc.get("city")
        region = ats_loc.get("state") or ats_loc.get("province") or loc.get("state")
        where = ", ".join(x for x in (city, region, ats_loc.get("country")) if x)
        out.append(dict(external_id=str(j.get("id", "")), title=(j.get("jobOpeningName") or "").strip(), location=where,
                        remote=str(j.get("locationType")) == "1" or bool(j.get("isRemote")) or _remote(where),
                        workplace={"0": "onsite", "1": "remote", "2": "hybrid"}.get(str(j.get("locationType"))),
                        department=j.get("departmentLabel") or "", employment_type=j.get("employmentStatusLabel") or "",
                        url=f"https://{token}.bamboohr.com/careers/{j['id']}" if token and j.get("id") else "", description=""))
    return [j for j in out if j["external_id"]]


def parse_teamtailor(data: dict) -> list[dict]:
    out = []
    for j in data.get("items", []) if isinstance(data, dict) else []:
        jp = j.get("_jobposting") if isinstance(j.get("_jobposting"), dict) else {}
        places = []
        for p in jp.get("jobLocation") or []:
            a = (p or {}).get("address") or {}
            place = ", ".join(x for x in (a.get("addressLocality"), a.get("addressCountry")) if x)
            if place and place not in places:
                places.append(place)
        emp = jp.get("employmentType")
        emp = ", ".join(emp) if isinstance(emp, list) else (emp or "")
        ident = (jp.get("identifier") or {}).get("value") or j.get("id") or ""
        out.append(dict(external_id=str(ident), title=(j.get("title") or "").strip(), location="; ".join(places[:3]),
                        remote=jp.get("jobLocationType") == "TELECOMMUTE" or _remote(j.get("title")), workplace=jp.get("jobLocationType"),
                        employment_type=emp.replace("_", " ").title(), url=j.get("url", ""),
                        description=_desc(j.get("content_html") or jp.get("description") or "")[:8000],
                        posted_at=_dt(j.get("date_published") or jp.get("datePosted"))))
    return [j for j in out if j["external_id"]]


def _safe_xml(text: str):
    """Boards are untrusted input: refuse documents that declare entities or a DOCTYPE (entity-expansion attacks)."""
    up = text.upper()
    if "<!DOCTYPE" in up or "<!ENTITY" in up:
        raise ValueError("XML with a DOCTYPE or entity declaration is not accepted")
    import xml.etree.ElementTree as ET
    return ET.fromstring(text)


def parse_personio(data: dict) -> list[dict]:
    token, out = data.get("_token", ""), []
    root = _safe_xml(data.get("xml", "") or "<x/>")
    for p in root.iter("position"):
        def g(tag):
            e = p.find(tag)
            return (e.text or "").strip() if e is not None and e.text else ""
        offices = [g("office")] + [(e.text or "").strip() for e in p.findall("additionalOffices/office")]
        offices = list(dict.fromkeys(o for o in offices if o))
        text = " ".join((d.findtext("value") or "") for d in p.findall("jobDescriptions/jobDescription"))
        title, pid = g("name"), g("id")
        out.append(dict(external_id=pid, title=title, location="; ".join(offices[:3]),
                        remote=_remote(*offices, title), department=g("department") or g("recruitingCategory"),
                        employment_type=" ".join(x for x in (g("schedule"), g("employmentType")) if x).replace("-", " ").replace("_", " ").title(),
                        url=f"https://{token}.jobs.personio.de/job/{pid}" if token and pid else "",
                        description=_desc(text)[:8000], posted_at=_dt(g("createdAt"))))
    return [j for j in out if j["external_id"]]


WD_TOKEN = re.compile(r"([a-z0-9][a-z0-9-]{0,62})\.(wd\d{1,3})\.([A-Za-z0-9_-]{1,100})")
WD_URL = re.compile(r"https?://([a-z0-9][a-z0-9-]{0,62})\.(wd\d{1,3})\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?([A-Za-z0-9_-]{1,100})", re.I)
WD_PAGE, WD_MAX_PAGES = 20, 50     # Workday rejects pages over 20; 50 pages = the newest 1000 postings per employer


def workday_token(raw: str) -> str:
    """'tenant.wdN.site' from either that form or a careers-page URL like https://nvidia.wd5.myworkdayjobs.com/en-US/Site.
    The parts end up in a hostname, so only plain letters, digits and hyphens are accepted. Raises ValueError otherwise."""
    raw = (raw or "").strip()
    m = WD_URL.match(raw)
    if m:
        return f"{m.group(1).lower()}.{m.group(2).lower()}.{m.group(3)}"
    m = WD_TOKEN.fullmatch(raw)
    if m:
        return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    raise ValueError("Paste the Workday careers page address, e.g. https://acme.wd5.myworkdayjobs.com/en-US/Careers")


def clean_token(ats: str, raw: str) -> str:
    """What the admin form / cli stores as the board token. Raises ValueError for an unusable Workday address."""
    return workday_token(raw) if ats == "workday" else (raw or "").strip().strip("/").split("/")[-1]


def _wd(token: str) -> tuple[str, str, str]:
    tenant, wd, site = workday_token(token).split(".", 2)
    return tenant, wd, site


def _wd_base(token: str) -> str:
    tenant, wd, _ = _wd(token)
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


def parse_workday(data: dict) -> list[dict]:
    """List postings only: the text, exact date and full location list come from the per-job detail (workday_detail)."""
    token, out = data.get("_token", ""), []
    base, site = _wd_base(token), _wd(token)[2]
    for j in data.get("jobPostings", []) if isinstance(data, dict) else []:
        path = j.get("externalPath") or ""
        bullets = j.get("bulletFields") or []
        ext = str(bullets[0]) if bullets else (hashlib.sha1(path.encode()).hexdigest()[:24] if path else "")
        loc = j.get("locationsText") or ""
        if re.fullmatch(r"\d+ Locations?", loc):      # "6 Locations": the detail call lists them
            loc = ""
        out.append(dict(external_id=ext, title=(j.get("title") or "").strip(), location=loc,
                        remote=str(j.get("remoteType") or "").lower() == "remote" or _remote(loc), workplace=j.get("remoteType"),
                        url=f"{base}/{site}{path}" if path else "", description=""))
    return [j for j in out if j["external_id"]]


def parse_breezy(data: list) -> list[dict]:
    out = []
    for j in data if isinstance(data, list) else []:
        loc = j.get("location") or {}
        name = loc.get("name") or ""
        out.append(dict(external_id=str(j.get("id") or j.get("friendly_id") or ""), title=(j.get("name") or "").strip(), location=name,
                        remote=bool(loc.get("is_remote")) or _remote(name), department=j.get("department") or "",
                        employment_type=(j.get("type") or {}).get("name", "") or "", url=j.get("url", ""),
                        description=_desc(j.get("description", ""))[:8000], posted_at=_dt(j.get("published_date"))))
    return [j for j in out if j["external_id"]]


ATS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true&pay_transparency=true", parse_greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{t}?mode=json", parse_lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true", parse_ashby),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{t}?details=true", parse_workable),
    "recruitee": ("https://{t}.recruitee.com/api/offers/", parse_recruitee),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{t}/postings?limit={n}&offset={o}", parse_smartrecruiters),
    "bamboohr": ("https://{t}.bamboohr.com/careers/list", parse_bamboohr),
    "teamtailor": ("https://{t}.teamtailor.com/jobs.json", parse_teamtailor),
    "personio": ("https://{t}.jobs.personio.de/xml?language=en", parse_personio),
    "breezy": ("https://{t}.breezy.hr/json", parse_breezy),
    "workday": ("", parse_workday),     # POST search under https://{tenant}.{wdN}.myworkdayjobs.com; see _fetch_workday
}
TEXT_FEEDS = {"personio"}            # the response is XML text, not JSON
JSON_HEADERS = {"bamboohr": {"Accept": "application/json"}}
# Only these are probed by guessing a slug from a company name. Recruitee/Workable/SmartRecruiters answer for look-alike
# slugs far too often, so those boards are added deliberately (admin form or `cli add`), never by name guessing.
GUESSABLE = ("greenhouse", "lever", "ashby")
SR_PAGE, SR_MAX_PAGES = 100, 30      # SmartRecruiters pages: up to 3000 postings per company, polite to the API
DETAIL_MAX_FAILURES = 5              # consecutive empty detail answers before a company's detail step gives up
DETAIL_PER_RUN = 60                  # postings whose text is fetched individually, per company per scrape


class JobList(list):
    """Parsed jobs. partial=True means the board had more postings than we fetched, so absence proves nothing."""
    partial = False


def _get(c: httpx.Client, url: str, headers: dict | None = None, json: dict | None = None, tries: int = 3) -> httpx.Response:
    for attempt in range(tries):   # be polite: back off on rate limiting / transient server errors
        r = c.post(url, headers=headers, json=json) if json is not None else c.get(url, headers=headers)
        if r.status_code != 429 and r.status_code < 500:
            break
        if attempt < tries - 1:
            time.sleep(2 * (attempt + 1))
    r.raise_for_status()
    return r


def _fetch_smartrecruiters(c: httpx.Client, token: str, light: bool) -> tuple[dict, bool]:
    url = ATS["smartrecruiters"][0]
    if light:
        return _get(c, url.format(t=token, n=1, o=0)).json(), False
    content, partial = [], False
    for page in range(SR_MAX_PAGES):
        data = _get(c, url.format(t=token, n=SR_PAGE, o=page * SR_PAGE)).json()
        got = data.get("content") or []
        content += got
        total = data.get("totalFound", 0)
        if len(content) >= total or not got:
            break
        if page == SR_MAX_PAGES - 1:
            partial = True
    return {"content": content}, partial


def sr_description(c: httpx.Client, token: str, posting_id: str) -> str:
    """SmartRecruiters keeps the job text on a per-posting endpoint; join its sections."""
    try:
        r = _get(c, f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{posting_id}", tries=1)
        sections = ((r.json().get("jobAd") or {}).get("sections")) or {}
    except Exception:
        return ""
    order = ("jobDescription", "qualifications", "additionalInformation")
    return _desc("<br><br>".join((sections.get(k) or {}).get("text", "") for k in order))[:8000]


def bamboo_detail(c: httpx.Client, token: str, posting_id: str) -> dict:
    """BambooHR's list has no job text or posting date; both live on the per-job detail endpoint."""
    try:
        r = _get(c, f"https://{token}.bamboohr.com/careers/{posting_id}/detail", JSON_HEADERS["bamboohr"], tries=1)
        job = (r.json().get("result") or {}).get("jobOpening") or {}
    except Exception:
        return {}
    return {"description": _desc(job.get("description", ""))[:8000], "posted_at": _dt(job.get("datePosted"))}


def _fetch_workday(c: httpx.Client, token: str, light: bool) -> tuple[dict, bool]:
    tenant, _, site = _wd(token)
    url = f"{_wd_base(token)}/wday/cxs/{tenant}/{site}/jobs"
    body = lambda off, n: {"appliedFacets": {}, "limit": n, "offset": off, "searchText": ""}
    if light:
        return {"jobPostings": _get(c, url, json=body(0, 1)).json().get("jobPostings", [])}, False
    postings, total, partial = [], 0, False
    for page in range(WD_MAX_PAGES):
        data = _get(c, url, json=body(page * WD_PAGE, WD_PAGE)).json()
        got = data.get("jobPostings") or []
        postings += got
        total = total or data.get("total", 0)       # Workday reports the total on the first page only
        if len(got) < WD_PAGE or (total and len(postings) >= total):
            break
        if page == WD_MAX_PAGES - 1:
            partial = True
    return {"jobPostings": postings}, partial


def workday_detail(c: httpx.Client, token: str, row) -> dict:
    """The per-job endpoint holds the description, exact start date, every location and the remote/time type."""
    tenant, _, site = _wd(token)
    path = row.url.split(f"/{site}", 1)[1] if f"/{site}" in row.url else ""
    if not path:
        return {}
    try:
        info = _get(c, f"{_wd_base(token)}/wday/cxs/{tenant}/{site}{path}", tries=1).json().get("jobPostingInfo") or {}
    except Exception:
        return {}
    places = [p for p in [info.get("location")] + list(info.get("additionalLocations") or []) if p]
    return {"description": _desc(info.get("jobDescription", ""))[:8000], "posted_at": _dt(info.get("startDate")),
            "location": "; ".join(dict.fromkeys(places[:3])) or None,
            "remote": str(info.get("remoteType") or "").lower() == "remote", "workplace": info.get("remoteType"),
            "employment_type": info.get("timeType") or None}


# ATSs whose list endpoint lacks the job text: fetched one posting at a time, DETAIL_PER_RUN per scrape.
# Each takes (client, board token, job row) and returns the fields it found.
DETAIL = {
    "smartrecruiters": lambda c, t, r: {"description": sr_description(c, t, r.external_id)},
    "bamboohr": lambda c, t, r: bamboo_detail(c, t, r.external_id),
    "workday": workday_detail,
}
# Fields the detail call knows better than the list: once a job has its text, list scrapes must not overwrite them
ENRICHED_KEYS = {"workday": ("location", "remote", "employment_type", "countries", "job_type", "workplace", "remote_regions")}
SCRAPE_BATCH_SIZE = config.SCRAPE_BATCH_SIZE


def fetch_jobs(ats: str, token: str, client: httpx.Client | None = None, light: bool = False) -> JobList:
    url, parser = ATS[ats]
    if light and ats == "greenhouse":   # existence probes don't need job descriptions or pay blurbs (Greenhouse's are huge)
        url = url.split("?", 1)[0]
    c = client or httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True)
    try:
        partial = False
        if ats == "smartrecruiters":
            payload, partial = _fetch_smartrecruiters(c, token, light)
        elif ats == "workday":
            payload, partial = _fetch_workday(c, token, light)
            payload["_token"] = token
        else:
            r = _get(c, url.format(t=token), JSON_HEADERS.get(ats))
            if ats in TEXT_FEEDS:
                payload = {"xml": r.text}
            else:
                payload = r.json()
            if isinstance(payload, dict):
                payload["_token"] = token      # parsers that build links need the board token
        out = JobList(j for j in parser(payload) if j["title"] and j["url"])
        out.partial = partial
        return out
    finally:
        if client is None:
            c.close()


def clip(model, d: dict) -> dict:
    """Trim strings to their column length. SQLite ignores VARCHAR limits but Postgres rejects the row,
    and real job data has e.g. 400-character multi-office location lists."""
    for col in model.__table__.columns:
        n = getattr(col.type, "length", None)
        if n and isinstance(d.get(col.name), str) and len(d[col.name]) > n:
            d[col.name] = d[col.name][:n]
    return d


async def scrape_company(db: AsyncSession, company: Company, client: httpx.Client | None = None) -> int:
    """Upsert a company's jobs; jobs that vanished from the board are deactivated."""
    now = datetime.utcnow()
    if company.ats == "direct":
        return 0
    try:
        jobs = await asyncio.to_thread(fetch_jobs, company.ats, company.board_token, client)   # blocking HTTP: off the event loop
    except Exception as e:  # network / 404 / bad token: record, never crash the run
        prev = company.last_error or ""
        n = 1
        if prev.startswith("fail:"):
            try:
                n = int(prev.split(":", 2)[1]) + 1
            except (IndexError, ValueError):
                n = 1
        company.last_error = f"fail:{n}:{type(e).__name__}: {e}"[:300]
        company.last_scraped = now
        await db.commit()
        return 0
    existing = {j.external_id: j for j in (await db.scalars(select(Job).where(Job.company_id == company.id)))}
    partial = getattr(jobs, "partial", False)
    if not jobs and not partial:
        # A board that suddenly lists nothing is far more often an API blip than every role closing at once.
        # Keep the listings and retry; a second empty answer in a row is accepted as real.
        kept = sum(1 for r in existing.values() if r.active)
        if kept and not (company.last_error or "").startswith(EMPTY):
            company.last_error = f"{EMPTY}: board returned 0 jobs; keeping {kept} listings until it repeats"
            company.last_scraped = now
            await db.commit()
            return 0
    seen = set()
    # roles of this employer that closed earlier, by (title, location): when the same role appears again it is a repost
    closed: dict[tuple, int] = {}
    for r in existing.values():
        if not r.active:
            k = ((r.title or "").strip().lower(), (r.location or "").strip().lower())
            closed[k] = max(closed.get(k, 0), (r.repost_count or 0) + 1)
    pending_new: list[Job] = []
    for d in jobs:
        d["company_name"] = company.name
        clip(Job, d)
        d.update(classify.classify(d))
        d["dup_key"] = dedupe.make_key(company.id, d["title"], d.get("description"), d["external_id"])
        seen.add(d["external_id"])
        row = existing.get(d["external_id"])
        if row:
            if not row.active:       # the very same posting came back after it had closed
                row.repost_count = (row.repost_count or 0) + 1
            for k, v in d.items():
                if k == "description" and not v and row.description:   # list-only sources (SmartRecruiters) send none
                    continue
                if row.description and k in ENRICHED_KEYS.get(company.ats, ()):
                    continue
                if k == "visa" and row.description and not d.get("description"):     # list-only sources carry no text to read it from
                    continue
                if k == "dup_key" and "description" in d and not d["description"] and row.description:
                    continue
                setattr(row, k, v)
            row.last_seen, row.active = now, True
        else:
            d["repost_count"] = closed.get(((d.get("title") or "").strip().lower(), (d.get("location") or "").strip().lower()), 0)
            row = Job(company_id=company.id, first_seen=now, last_seen=now, **d)
            pending_new.append(row)
            existing[d["external_id"]] = row
        if len(pending_new) >= SCRAPE_BATCH_SIZE:
            db.add_all(pending_new)
            await db.flush()
            await db.commit()
            pending_new.clear()
    if pending_new:
        db.add_all(pending_new)
        await db.flush()
        await db.commit()
    if company.ats in DETAIL:
        await asyncio.to_thread(_fill_details, company, existing, seen, client)
    if not partial:
        # Missing from the board is not closed on the first miss: wait CLOSE_GRACE_HOURS (default two scrape cycles)
        # so one flaky response cannot wipe listings. last_seen is only refreshed when a job is actually seen.
        closed_before = now - timedelta(hours=config.CLOSE_GRACE_HOURS)
        for ext, row in existing.items():
            if ext not in seen and row.active and row.last_seen is not None and row.last_seen <= closed_before:
                row.active = False
    company.last_scraped, company.last_error, company.job_count = now, "", len(jobs)
    await db.commit()
    return len(jobs)


EMPTY = "EmptyResponse"


def reclassify(row: Job) -> None:
    """Recompute the derived filter columns from the row's own text."""
    got = classify.classify(dict(location=row.location, employment_type=row.employment_type, title=row.title,
                                 salary_min=row.salary_min, salary_max=row.salary_max, salary_currency=row.salary_currency,
                                 salary_period=row.salary_period, description=row.description, remote=row.remote,
                                 workplace=row.workplace))      # the stored workplace is the source's own word (or an earlier reading of the text)
    row.countries, row.job_type, row.seniority = got["countries"], got["job_type"], got["seniority"]
    row.pay_min_usd, row.pay_max_usd = got["pay_min_usd"], got["pay_max_usd"]
    row.workplace, row.remote, row.remote_regions, row.visa = got["workplace"], got["remote"], got["remote_regions"], got["visa"]


def _fill_details(company: Company, rows: dict, seen: set, client: httpx.Client | None) -> None:
    todo = [r for ext, r in rows.items() if ext in seen and not r.description][:DETAIL_PER_RUN]
    if not todo:
        return
    fetch = DETAIL[company.ats]
    c = client or httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True)
    failures = 0
    try:
        for r in todo:
            if failures >= DETAIL_MAX_FAILURES:    # the detail endpoint is down or blocking us: stop, retry next cycle
                break
            got = fetch(c, company.board_token, r)
            failures = 0 if got.get("description") else failures + 1
            if got.get("posted_at") and not r.posted_at:
                r.posted_at = got["posted_at"]     # so the max-age filter sees the real posting date
            if got.get("description"):
                r.description = got["description"]
                for k in ENRICHED_KEYS.get(company.ats, ()):
                    if got.get(k) is not None:
                        setattr(r, k, got[k])
                reclassify(r)       # the detail call supplies the text (visa), and may supply the real locations / workplace / job type
                r.dup_key = dedupe.make_key(company.id, r.title, r.description, r.external_id)
    finally:
        if client is None:
            c.close()


async def scrape_all(db: AsyncSession, only_stale_hours: float | None = None) -> dict:
    stats = {"companies": 0, "jobs": 0, "errors": 0}
    q = select(Company).where(Company.active.is_(True), Company.ats != "direct")     # direct employers have no board to fetch
    cutoff = datetime.utcnow() - timedelta(hours=only_stale_hours) if only_stale_hours else None
    with httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True) as client:
        for c in (await db.scalars(q)).all():
            if c.last_scraped:
                # healthy boards wait out the full interval; failed ones retry after a short backoff
                fresh_after = (datetime.utcnow() - RETRY_AFTER) if c.last_error else cutoff
                if fresh_after and c.last_scraped > fresh_after:
                    continue
            n = await scrape_company(db, c, client)
            stats["companies"] += 1
            stats["jobs"] += n
            stats["errors"] += 1 if c.last_error else 0
    # retire jobs nobody has refreshed in a long time
    stale = datetime.utcnow() - timedelta(days=config.JOB_STALE_DAYS)
    # (direct postings are never refreshed by a scrape: they end on their own expires_at)
    await db.execute(update(Job).where(Job.last_seen < stale, or_(Job.source.is_(None), Job.source != "direct")).values(active=False))
    await db.commit()
    # Soft-disable boards that hard-404 or fail repeatedly with zero active jobs
    pruned = 0
    for c in (await db.scalars(select(Company).where(Company.active.is_(True), Company.ats != "direct"))):
        if not c.last_error or not c.last_scraped:
            continue
        active_jobs = sum(1 for j in (await db.scalars(select(Job).where(Job.company_id == c.id, Job.active.is_(True)))))
        err = (c.last_error or "").lower()
        hard = any(x in err for x in ("404", "not found", "unknown board", "invalid", "no such"))
        fails = 0
        if err.startswith("fail:"):
            try:
                fails = int(err.split(":", 2)[1])
            except (IndexError, ValueError):
                fails = 0
        if active_jobs == 0 and (hard or fails >= 3):
            c.active = False
            pruned += 1
    if pruned:
        await db.commit()
        stats["pruned"] = pruned
    return stats
