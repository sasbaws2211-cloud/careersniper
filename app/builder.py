"""The free résumé and CV builder: checks what the form sends, builds a `docfmt.Resume` from it, and makes the preview and the files.

Nothing is stored. The browser keeps the draft; the server only turns it into a preview or a download and forgets it.

The résumé is built directly as a `docfmt.Resume` instead of being written out as text for `docfmt.parse_resume` to read back: that parser guesses
(a job title of "CEO" looks like a heading, a bullet of "SQL" too), and a builder knows exactly what each field is.
"""
from __future__ import annotations

import re
import unicodedata

from markupsafe import Markup

from . import docfmt
from .docfmt import Entry, Resume, Section

MODES = ("resume", "cv")
FORMATS = {"pdf": "application/pdf", "docx": docfmt.DOCX_MEDIA, "txt": "text/plain; charset=utf-8"}

# Limits keep one request small and a document sensible. Going over one is an error that says which, never a silent cut.
MAX_JSON_BYTES = 120_000
MAX_NAME, MAX_SHORT, MAX_LINK, MAX_SUMMARY, MAX_BULLET = 80, 100, 200, 1500, 400
MAX_LINKS, MAX_EXPERIENCE, MAX_EDUCATION, MAX_SKILL_GROUPS, MAX_PROJECTS, MAX_EXTRAS = 5, 15, 8, 10, 8, 8
MAX_BULLETS, MAX_EXTRA_ITEMS, MAX_SKILL_ITEMS, MAX_REFERENCES = 15, 30, 500, 600

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f  ]")
_SPACES = re.compile(r"\s+")
_BULLET_MARK = re.compile(r"^\s*(?:[-•·▪●◦‣*–—]|\d{1,2}[.)])\s+")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")


class BuilderError(ValueError):
    """Something in the form cannot be used. The message is written for the person filling it in."""


def _s(value, limit: int, label: str) -> str:
    """One line of text: control characters removed, whitespace collapsed, and an error when it is longer than `limit`."""
    if value is None:
        return ""
    text = _SPACES.sub(" ", _CONTROL.sub(" ", value if isinstance(value, str) else str(value))).strip()
    if len(text) > limit:
        raise BuilderError(f"{label} is too long: the limit is {limit} characters.")
    return text


def _lines(value, count: int, limit: int, label: str) -> list[str]:
    """A list of short lines, from a list or from text with one line each. A leading bullet mark is dropped; blank lines are ignored."""
    if value is None:
        return []
    raw = value.split("\n") if isinstance(value, str) else (value if isinstance(value, list) else [])
    out = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = _s(_BULLET_MARK.sub("", item, count=1), limit, label)
        if text:
            out.append(text)
    if len(out) > count:
        raise BuilderError(f"{label} has too many lines: the limit is {count}.")
    return out


def _list_of(value, count: int, label: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise BuilderError(f"{label} is not in the form we expect.")
    if len(value) > count:
        raise BuilderError(f"{label} has too many entries: the limit is {count}.")
    return value


def dates(start: str, end: str) -> str:
    """'Mar 2022 – Present' from the two boxes; a single date stands alone."""
    if start and end:
        return f"{start} – {end}"
    return start or end


def clean(data, mode: str) -> dict:
    """The form's data checked and tidied, or a BuilderError. Only the name is required."""
    if mode not in MODES:
        raise BuilderError("Choose résumé or CV.")
    if not isinstance(data, dict):
        raise BuilderError("The form data is not in the form we expect.")
    name = _s(data.get("name"), MAX_NAME, "Your name")
    c = data.get("contact") if isinstance(data.get("contact"), dict) else {}
    email = _s(c.get("email"), MAX_SHORT, "Your email")
    if email and not _EMAIL.match(email):
        raise BuilderError("That email address does not look right.")
    out = {
        "name": name,
        "contact": {
            "email": email,
            "phone": _s(c.get("phone"), 40, "Your phone number"),
            "location": _s(c.get("location"), MAX_SHORT, "Your location"),
            "links": _lines(c.get("links"), MAX_LINKS, MAX_LINK, "Your links"),
        },
        "summary": _s(data.get("summary"), MAX_SUMMARY, "Your summary"),
        "experience": [], "education": [], "skills": [], "projects": [], "extras": [],
        "references": _s(data.get("references"), MAX_REFERENCES, "References") if mode == "cv" else "",
    }
    for e in _list_of(data.get("experience"), MAX_EXPERIENCE, "Work experience"):
        item = {"title": _s(e.get("title"), MAX_SHORT, "A job title"), "company": _s(e.get("company"), MAX_SHORT, "A company"),
                "location": _s(e.get("location"), MAX_SHORT, "A job location"), "start": _s(e.get("start"), 30, "A start date"),
                "end": _s(e.get("end"), 30, "An end date"), "bullets": _lines(e.get("bullets"), MAX_BULLETS, MAX_BULLET, "A list of achievements")}
        if item["title"] or item["company"]:
            out["experience"].append(item)
    for e in _list_of(data.get("education"), MAX_EDUCATION, "Education"):
        item = {"degree": _s(e.get("degree"), MAX_SHORT, "A degree"), "school": _s(e.get("school"), MAX_SHORT, "A school"),
                "start": _s(e.get("start"), 30, "A start date"), "end": _s(e.get("end"), 30, "An end date"),
                "details": _lines(e.get("details"), MAX_BULLETS, MAX_BULLET, "Education details")}
        if item["degree"] or item["school"]:
            out["education"].append(item)
    for e in _list_of(data.get("skills"), MAX_SKILL_GROUPS, "Skills"):
        item = {"label": _s(e.get("label"), 40, "A skills heading"), "items": _s(e.get("items"), MAX_SKILL_ITEMS, "A list of skills")}
        if item["items"]:
            out["skills"].append(item)
    for e in _list_of(data.get("projects"), MAX_PROJECTS, "Projects"):
        item = {"title": _s(e.get("title"), MAX_SHORT, "A project name"), "link": _s(e.get("link"), MAX_LINK, "A project link"),
                "start": _s(e.get("start"), 30, "A start date"), "end": _s(e.get("end"), 30, "An end date"),
                "bullets": _lines(e.get("bullets"), MAX_BULLETS, MAX_BULLET, "A project description")}
        if item["title"]:
            out["projects"].append(item)
    for e in _list_of(data.get("extras"), MAX_EXTRAS, "Extra sections"):
        item = {"title": _s(e.get("title"), 40, "A section title"), "items": _lines(e.get("items"), MAX_EXTRA_ITEMS, MAX_BULLET, "A section")}
        if item["title"] and item["items"]:
            out["extras"].append(item)
    return out


def has_content(d: dict) -> bool:
    return bool(d["summary"] or d["experience"] or d["education"] or d["skills"] or d["projects"] or d["extras"] or d["references"])


def _section(title: str) -> Section:
    return Section(title)


def build(d: dict, mode: str) -> Resume:
    """A `docfmt.Resume` from tidied data (see `clean`). A résumé leads with experience; a CV with education and says 'profile'."""
    c = d["contact"]
    r = Resume(name=d["name"], contact=[x for x in (c["email"], c["phone"], c["location"], *c["links"]) if x])
    cv = mode == "cv"

    def paragraph(title: str, text: str) -> None:
        if text:
            s = _section(title)
            s.paragraphs.append(text)
            s.order.append(("p", 0))
            r.sections.append(s)

    def entries(title: str, items: list[Entry]) -> None:
        if items:
            s = _section(title)
            for e in items:
                s.entries.append(e)
                s.order.append(("e", len(s.entries) - 1))
            r.sections.append(s)

    def experience() -> None:
        entries("WORK EXPERIENCE" if cv else "EXPERIENCE", [
            Entry(title=e["title"] or e["company"], org=" | ".join(x for x in ((e["company"] if e["title"] else ""), e["location"]) if x),
                  dates=dates(e["start"], e["end"]), bullets=e["bullets"]) for e in d["experience"]])

    def education() -> None:
        entries("EDUCATION", [Entry(title=e["degree"] or e["school"], org=e["school"] if e["degree"] else "", dates=dates(e["start"], e["end"]),
                                    bullets=e["details"]) for e in d["education"]])

    def skills() -> None:
        if d["skills"]:
            s = _section("SKILLS")
            for g in d["skills"]:
                if g["label"]:
                    s.labelled.append((g["label"], g["items"]))
                    s.order.append(("l", len(s.labelled) - 1))
                else:
                    s.bullets.append(g["items"])
                    s.order.append(("b", len(s.bullets) - 1))
            r.sections.append(s)

    def projects() -> None:
        entries("PROJECTS", [Entry(title=p["title"], org=p["link"], dates=dates(p["start"], p["end"]), bullets=p["bullets"]) for p in d["projects"]])

    paragraph("PROFILE" if cv else "SUMMARY", d["summary"])
    if cv:
        education()
        experience()
        projects()
        skills()
    else:
        experience()
        projects()
        skills()
        education()
    for x in d["extras"]:
        s = _section(x["title"].upper())
        s.bullets = list(x["items"])
        s.order = [("b", i) for i in range(len(s.bullets))]
        r.sections.append(s)
    paragraph("REFERENCES", d["references"])
    return r


def prepare(data, mode: str, need_content: bool = False) -> Resume:
    """Check the form data and build the résumé, or raise a BuilderError. Downloads also need something besides a name."""
    d = clean(data, mode)
    if not d["name"]:
        raise BuilderError("Add your name first.")
    if need_content and not has_content(d):
        raise BuilderError("Add some content first: a summary, a job, your education or your skills.")
    return build(d, mode)


def render_html(data, mode: str, template: str) -> Markup:
    return docfmt.resume_html(prepare(data, mode), template)


_TRANSLIT = str.maketrans({"Ł": "L", "ł": "l", "Ø": "O", "ø": "o", "Đ": "D", "đ": "d", "ß": "ss", "Æ": "AE", "æ": "ae",
                           "Œ": "OE", "œ": "oe", "Þ": "Th", "þ": "th", "İ": "I", "ı": "i"})


def slug(name: str) -> str:
    """A file-name-safe version of a name: ASCII letters and digits with hyphens."""
    text = unicodedata.normalize("NFKD", (name or "").translate(_TRANSLIT)).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40]


def filename(name: str, mode: str, fmt: str) -> str:
    return f"{slug(name) or 'my'}-{'cv' if mode == 'cv' else 'resume'}.{fmt}"


def make_file(data, mode: str, fmt: str, template: str, paper: str) -> tuple[bytes | str, str, str]:
    """(the file's content, its name, its content type) for a download in `fmt` ('pdf', 'docx' or 'txt')."""
    if fmt not in FORMATS:
        raise BuilderError("Choose PDF, Word or text.")
    r = prepare(data, mode, need_content=True)
    paper = "letter" if (paper or "").lower() == "letter" else "a4"
    title = f"{r.name} {'CV' if mode == 'cv' else 'résumé'}"
    if fmt == "pdf":
        body: bytes | str = docfmt.resume_pdf(r, paper=paper, title=title, template=template)
    elif fmt == "docx":
        body = docfmt.resume_docx(r, paper=paper, title=title, template=template)
    else:
        body = docfmt.resume_txt(r, template)
    return body, filename(r.name, mode, fmt), FORMATS[fmt]


# A fictional example, so a visitor can see what a finished document looks like before typing their own.
SAMPLE = {
    "resume": {
        "name": "Jane Sample", "contact": {"email": "jane.sample@example.com", "phone": "+44 7700 900123", "location": "London, United Kingdom", "links": []},
        "summary": "Backend engineer with six years of experience building and scaling web services in Ruby, Go and PostgreSQL.",
        "experience": [
            {"title": "Senior Backend Engineer", "company": "Northwind Labs", "location": "London", "start": "Mar 2022", "end": "Present",
             "bullets": ["Own the permissions service of a developer platform used by 40,000 teams.", "Cut p95 API latency from 800 ms to 210 ms by tuning PostgreSQL queries and adding caching."]},
            {"title": "Backend Engineer", "company": "Fabrikam Software", "location": "Remote", "start": "Jun 2019", "end": "Feb 2022",
             "bullets": ["Built REST and GraphQL endpoints for a billing platform.", "Wrote the integration test suite that made daily deploys possible."]}],
        "education": [{"degree": "BSc Computer Science", "school": "University of Leeds", "start": "2015", "end": "2018", "details": []}],
        "skills": [{"label": "Languages", "items": "Ruby, Go, SQL"}, {"label": "Tools", "items": "PostgreSQL, Redis, Docker"}],
        "projects": [], "extras": [], "references": "",
    },
    "cv": {
        "name": "Jane Sample", "contact": {"email": "jane.sample@example.com", "phone": "+44 7700 900123", "location": "Leeds, United Kingdom", "links": []},
        "summary": "Research engineer with a background in distributed systems, now working on reliable data pipelines.",
        "experience": [{"title": "Research Engineer", "company": "Northwind Labs", "location": "Leeds", "start": "Sep 2021", "end": "Present",
                        "bullets": ["Built a streaming pipeline that processes 2 million events a day.", "Supervised two graduate interns."]}],
        "education": [{"degree": "PhD Computer Science", "school": "University of Leeds", "start": "2017", "end": "2021", "details": ["Thesis: fault tolerance in stream processing."]},
                      {"degree": "MSc Software Engineering", "school": "University of Manchester", "start": "2015", "end": "2016", "details": []}],
        "skills": [{"label": "Languages", "items": "Python, Go, SQL"}],
        "projects": [],
        "extras": [{"title": "Publications", "items": ["Sample, J. (2021). Fault tolerance in stream processing. Example Journal of Systems, 12(3)."]},
                   {"title": "Languages", "items": ["English (native)", "French (intermediate)"]}],
        "references": "Available on request.",
    },
}
