"""Résumés and cover letters as properly laid-out documents: on the page, as plain text, and as a PDF.

The AI (and the member, when they edit) works with plain text. This module reads that text into a small structure, a name, a contact
line, sections, roles with dates and bullets, and lays it out three ways:

    resume_html / letter_html   the preview on the site and the print page
    resume_txt  / letter_txt    the .txt download: headings underlined, bullets with a hanging indent, wrapped at 78 columns
    resume_pdf  / letter_pdf    a real PDF file (A4 or Letter): one clean sans-serif typeface, consistent spacing, clickable links

Nothing here changes the words. It only decides where line breaks, bold, rules and dates go, so a résumé reads the same on every
route. Text that does not fit the expected layout still comes out as readable paragraphs.
"""
from __future__ import annotations

import io
import re
import textwrap
from dataclasses import dataclass, field
from html import escape

from markupsafe import Markup

# ------------------------------------------------------------------ the templates
# Exactly three looks, shared by a résumé and a cover letter so the pair reads as one package. All are one column with no tables,
# icons or photos, which is what applicant-tracking systems read best. A template changes layout, never words.
TEMPLATES: tuple[tuple[str, str, str], ...] = (
    ("classic", "Classic", "Centred name, ruled section headings, sans-serif. The safe choice for most jobs."),
    ("modern", "Modern", "Left-aligned name and coloured headings. Suits tech, product and design roles."),
    ("compact", "Compact", "Serif type, tighter spacing. Fits a long career on fewer pages; suits formal fields."),
)
DEFAULT_TEMPLATE = "classic"
COOKIE = "cs_template"


def template_name(name: str | None) -> str:
    """A valid template name: the one given, or Classic when it is empty or unknown."""
    n = (name or "").strip().lower()
    return n if any(n == t[0] for t in TEMPLATES) else DEFAULT_TEMPLATE


# ------------------------------------------------------------------ reading the text
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_POINT = rf"(?:{_MONTH}\s+)?(?:19|20)\d{{2}}|present|current|now|ongoing|to date"
_RANGE = rf"(?:{_POINT})(?:\s*(?:-|\u2013|\u2014|to|until)\s*(?:{_POINT}))?"
_DATE_ONLY = re.compile(rf"^\W*(?P<d>{_RANGE})\W*$", re.I)
_DATE_TAIL = re.compile(rf"^(?P<rest>.*?)[\s,|(\[]+\(?(?P<d>{_RANGE})\)?\s*$", re.I)
_BULLET = re.compile(r"^\s*(?:[-\u2022\u00b7\u25aa\u25cf\u25e6\u2023*\u2013\u2014]|\d{1,2}[.)])\s+")
_LABEL = re.compile(r"^(?P<label>[A-Za-z][A-Za-z0-9 &/+#.'()\u2019-]{1,40}?)\s*:\s+(?P<rest>\S.*)$")
_URL = re.compile(r"(https?://[^\s<>\"')\]]+[^\s<>\"').,;\])])")
_EMAILRE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CONTACT_LABEL = re.compile(r"^\s*(?:e-?mail|phone|tel(?:ephone)?|mobile|cell|linkedin|github|website|web|portfolio|location|address)\s*:\s*", re.I)

_PARAGRAPH_SECTIONS = ("summary", "profile", "objective", "about", "overview", "statement")
_LABELLED_SECTIONS = ("skill", "technolog", "tool", "language", "competenc", "certification", "award", "interest", "expertise", "stack")


@dataclass
class Entry:
    title: str = ""
    org: str = ""
    dates: str = ""
    meta: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)


@dataclass
class Section:
    title: str
    paragraphs: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    labelled: list[tuple[str, str]] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    order: list[tuple[str, int]] = field(default_factory=list)       # how the parts were found, so the layout keeps the original order

    def empty(self) -> bool:
        return not self.order


@dataclass
class Resume:
    name: str = ""
    contact: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)


_KNOWN = re.compile(
    r"^(?:professional |career |executive )?(?:summary|profile|objective)$|^(?:work |professional |relevant |employment |career )?(?:experience|history)$|"
    r"^(?:key |core |technical |additional )?(?:qualifications|skills|competencies)$|^education(?: (?:and|&) \w+)?$|"
    r"^(?:projects?(?: experience)?|certifications?|languages?|awards?|references?|interests|publications|volunteer(?:ing)?(?: experience)?|achievements|training)$",
    re.I)


def _is_heading(line: str) -> bool:
    s = re.sub(r"^[#=*_\s]+|[#=*_:\s]+$", "", line.strip())
    if not s or len(s) > 48 or len(s) < 3 or s.endswith((".", ",", ";")):
        return False
    if _KNOWN.match(s):
        return True
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters) and len(s.split()) <= 6 and not _EMAILRE.search(s) and "http" not in s.lower()


def _clean_heading(line: str) -> str:
    return re.sub(r"^[#=*_\s]+|[#=*_:\s]+$", "", line.strip()).upper()


def _clean_contact(line: str) -> list[str]:
    s = _CONTACT_LABEL.sub("", line.strip())
    parts = [p.strip() for p in re.split(r"\s+[|\u2022\u00b7]\s+", s) if p.strip()]
    return parts or [s]


def _strip_bullet(line: str) -> str:
    return _BULLET.sub("", line, count=1).strip()


def _split_dates(line: str) -> tuple[str, str]:
    """('Software Engineer, Acme', '2021 - present') from the end of a line, or (line, '')."""
    m = _DATE_ONLY.match(line)
    if m:
        return "", m.group("d").strip()
    m = _DATE_TAIL.match(line)
    if m and m.group("rest").strip():
        return m.group("rest").strip(" ,|-\u2013\u2014"), m.group("d").strip()
    return line.strip(), ""


def _header_like(line: str) -> bool:
    s = line.strip()
    return bool(s) and not _BULLET.match(s) and len(s) <= 100 and len(s.split()) <= 16 and not s.endswith((".", "!", "?"))


def _parse_entry(lines: list[str]) -> Entry:
    e = Entry()
    heads: list[str] = []
    i = 0
    while i < len(lines) and len(heads) < 3 and _header_like(lines[i]):
        heads.append(lines[i].strip())
        i += 1
        if _split_dates(heads[-1])[1]:
            break
    rest = lines[i:]
    # a block that is a plain run of sentences has no header at all
    if len(heads) == len(lines) and len(heads) > 1 and not any(_split_dates(h)[1] for h in heads):
        heads, rest = heads[:1], lines[1:]
    texts = []
    for h in heads:
        t, d = _split_dates(h)
        if d and not e.dates:
            e.dates = d
        if t:
            texts.append(t)
    if texts:
        e.title = texts[0]
    if len(texts) > 1:
        e.org = texts[1]
    e.meta = texts[2:]
    e.bullets = [_strip_bullet(r) for r in rest if r.strip()]
    return e


def parse_resume(text: str) -> Resume:
    raw = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").split("\n")]
    while raw and not raw[0].strip():
        raw.pop(0)
    doc = Resume()
    i = 0
    if raw and not _KNOWN.match(raw[0].strip(" #*:")) and len(raw[0]) <= 70:
        doc.name = raw[0].strip(" #*")
        i = 1
    while i < len(raw) and not raw[i].strip():
        i += 1                                                   # the contact lines may follow after a blank line
    while (i < len(raw) and not _is_heading(raw[i]) and len(doc.contact) < 8 and raw[i].strip() and len(raw[i]) < 240
           and (_CONTACT_LABEL.match(raw[i]) or _EMAILRE.search(raw[i]) or _URL.search(raw[i])
                or re.search(r"\+?\d[\d ()/.-]{6,}", raw[i]) or len(raw[i].split()) <= 8)):
        doc.contact += _clean_contact(raw[i])
        i += 1
    current: Section | None = None
    block: list[str] = []
    blocks: list[tuple[Section | None, list[str]]] = []

    def flush():
        nonlocal block
        if block:
            blocks.append((current, block))
            block = []
    for line in raw[i:]:
        if _is_heading(line):
            flush()
            current = Section(_clean_heading(line))
            doc.sections.append(current)
        elif not line.strip():
            flush()
        else:
            block.append(line)
    flush()
    untitled: Section | None = None
    for sec, lines in blocks:
        if sec is None:                                          # text before the first heading
            if untitled is None:
                untitled = Section("")
                doc.sections.insert(0, untitled)
            sec = untitled
        key = sec.title.lower()
        if any(k in key for k in _PARAGRAPH_SECTIONS):
            sec.paragraphs.append(" ".join(_strip_bullet(l) for l in lines))
            sec.order.append(("p", len(sec.paragraphs) - 1))
        elif any(k in key for k in _LABELLED_SECTIONS):
            for l in lines:
                m = _LABEL.match(_strip_bullet(l))
                if m:
                    sec.labelled.append((m.group("label").strip(), m.group("rest").strip()))
                    sec.order.append(("l", len(sec.labelled) - 1))
                else:
                    sec.bullets.append(_strip_bullet(l))
                    sec.order.append(("b", len(sec.bullets) - 1))
        elif all(_BULLET.match(l) for l in lines) or ("qualification" in key and not any(_split_dates(l)[1] for l in lines)):
            for l in lines:
                sec.bullets.append(_strip_bullet(l))
                sec.order.append(("b", len(sec.bullets) - 1))
        else:
            sec.entries.append(_parse_entry(lines))
            sec.order.append(("e", len(sec.entries) - 1))
    doc.sections = [s for s in doc.sections if not s.empty()]
    return doc


def letterhead(resume_text: str) -> tuple[str, list[str]] | None:
    """(name, contact items) from the top of a résumé, to head a cover letter the same way, or None when there is nothing usable."""
    r = parse_resume(resume_text)
    return (r.name, r.contact) if r.name else None


# ------------------------------------------------------------------ letters
@dataclass
class Letter:
    paragraphs: list[list[str]] = field(default_factory=list)       # each paragraph is a list of lines (a closing keeps its line breaks)


def parse_letter(text: str) -> Letter:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    out: list[list[str]] = []
    for blk in re.split(r"\n\s*\n", t):
        lines = [ln.strip() for ln in blk.split("\n") if ln.strip()]
        if not lines:
            continue
        short_block = len(lines) <= 4 and all(len(l) <= 60 for l in lines)       # "Dear X," and "Sincerely, Name" keep their lines
        out.append(lines if short_block else [" ".join(lines)])
    return Letter(out)


# ------------------------------------------------------------------ HTML (preview and print)
def _link(text: str) -> str:
    """Escaped text with web addresses and e-mail addresses made clickable."""
    out, pos = [], 0
    spans = sorted([(m.start(), m.end(), m.group(0), "u") for m in _URL.finditer(text)] +
                   [(m.start(), m.end(), m.group(0), "e") for m in _EMAILRE.finditer(text) if not any(u.start() <= m.start() < u.end() for u in _URL.finditer(text))])
    for a, b, s, kind in spans:
        if a < pos:
            continue
        out.append(escape(text[pos:a]))
        href = s if kind == "u" else "mailto:" + s
        out.append(f'<a href="{escape(href, quote=True)}">{escape(s)}</a>')
        pos = b
    out.append(escape(text[pos:]))
    return "".join(out)


DOC_CSS = """
.doc{font-family:"Helvetica Neue",Helvetica,Arial,"Liberation Sans",sans-serif;font-size:10.5pt;line-height:1.45;color:#1a1a1a;background:#fff;
  max-width:8.27in;margin:0 auto;padding:.7in .75in;box-sizing:border-box}
.doc a{color:#1d4ed8;text-decoration:none}
.doc .name{font-size:22pt;font-weight:700;text-align:center;letter-spacing:.01em;margin:0 0 4px;color:#111}
.doc .contact{text-align:center;font-size:9.5pt;color:#444;margin:0 0 14px}
.doc .contact .sep{color:#9ca3af;padding:0 7px}
.doc h2{font-size:10.5pt;font-weight:700;letter-spacing:.09em;text-transform:uppercase;margin:16px 0 7px;padding-bottom:3px;border-bottom:1px solid #6b7280;color:#111}
.doc .entry{margin:0 0 10px;break-inside:avoid-page}
.doc .head{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.doc .title{font-weight:700}
.doc .dates{white-space:nowrap;color:#444;font-size:9.5pt}
.doc .org{font-style:italic;color:#333}
.doc .meta{color:#444;font-size:9.5pt}
.doc ul{margin:4px 0 0;padding-left:1.15em}
.doc li{margin:0 0 3px}
.doc p{margin:0 0 7px}
.doc .skill b{font-weight:700}
.doc.letter{font-size:11pt;line-height:1.6;padding:.9in 1in}
.doc.letter p{margin:0 0 12px}
.doc.letter .lines{margin:0 0 12px}
.doc.letter .rule{border:0;border-top:1px solid #6b7280;margin:6px 0 22px}
.doc.t-modern .name{text-align:left;color:#1e3a8a}
.doc.t-modern .contact{text-align:left}
.doc.t-modern h2{color:#1e3a8a;border-bottom:0;letter-spacing:.12em;margin:18px 0 6px;padding-bottom:0}
.doc.t-modern .org{font-style:normal;color:#444}
.doc.t-modern.letter .rule{border-top:2px solid #1e3a8a}
.doc.t-compact{font-family:Georgia,"Times New Roman",Times,"Liberation Serif",serif;font-size:9.8pt;line-height:1.35;padding:.55in .65in}
.doc.t-compact .name{font-size:18pt}
.doc.t-compact .contact{font-size:9pt;margin:0 0 8px;color:#333}
.doc.t-compact h2{font-size:9.4pt;margin:11px 0 5px;border-bottom:1px solid #111}
.doc.t-compact .entry{margin:0 0 6px}
.doc.t-compact li{margin:0 0 1px}
.doc.t-compact p{margin:0 0 5px}
.doc.t-compact.letter{font-size:10.5pt;line-height:1.5;padding:.8in .9in}
.doc.t-compact.letter p{margin:0 0 9px}
.doc.t-compact.letter .rule{border-top:1px solid #111}
@page{size:A4;margin:0}
@media print{.doc{padding:.75in .8in;max-width:none}.doc.letter{padding:1in}}
"""


def _contact_html(items: list[str]) -> str:
    """Contact items on lines of whole items, a divider between neighbours and none at a line's start or end."""
    lines, cur, size = [], [], 0
    for it in items:
        if cur and size + len(it) + 3 > 84:
            lines.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it) + 3
    if cur:
        lines.append(cur)
    return "".join("<div>" + '<span class="sep">|</span>'.join(f"<span>{_link(i)}</span>" for i in ln) + "</div>" for ln in lines)


def resume_html(text: str, template: str = DEFAULT_TEMPLATE) -> Markup:
    r = parse_resume(text)
    h = [f'<article class="doc resume t-{template_name(template)}">']
    if r.name:
        h.append(f'<div class="name">{escape(r.name)}</div>')
    if r.contact:
        h.append('<div class="contact">' + _contact_html(r.contact) + "</div>")
    for sec in r.sections:
        if sec.title:
            h.append(f"<h2>{escape(sec.title)}</h2>")
        pending_bullets: list[str] = []

        def flush():
            if pending_bullets:
                h.append("<ul>" + "".join(f"<li>{_link(b)}</li>" for b in pending_bullets) + "</ul>")
                pending_bullets.clear()
        for kind, idx in sec.order:
            if kind == "b":
                pending_bullets.append(sec.bullets[idx])
                continue
            flush()
            if kind == "p":
                h.append(f"<p>{_link(sec.paragraphs[idx])}</p>")
            elif kind == "l":
                label, rest = sec.labelled[idx]
                h.append(f'<p class="skill"><b>{escape(label)}:</b> {_link(rest)}</p>')
            elif kind == "e":
                e = sec.entries[idx]
                h.append('<div class="entry">')
                if e.title or e.dates:
                    h.append(f'<div class="head"><span class="title">{_link(e.title)}</span>' + (f'<span class="dates">{escape(e.dates)}</span>' if e.dates else "") + "</div>")
                if e.org:
                    h.append(f'<div class="org">{_link(e.org)}</div>')
                for m in e.meta:
                    h.append(f'<div class="meta">{_link(m)}</div>')
                if e.bullets:
                    h.append("<ul>" + "".join(f"<li>{_link(b)}</li>" for b in e.bullets) + "</ul>")
                h.append("</div>")
        flush()
    h.append("</article>")
    return Markup("".join(h))


def letter_html(text: str, head: tuple[str, list[str]] | None = None, template: str = DEFAULT_TEMPLATE) -> Markup:
    L = parse_letter(text)
    h = [f'<article class="doc letter t-{template_name(template)}">']
    if head and head[0]:
        h.append(f'<div class="name" style="text-align:left;font-size:18pt">{escape(head[0])}</div>')
        if head[1]:
            h.append('<div class="contact" style="text-align:left">' + _contact_html(head[1]) + "</div>")
        h.append('<hr class="rule">')
    for lines in L.paragraphs:
        if len(lines) == 1:
            h.append(f"<p>{_link(lines[0])}</p>")
        else:
            h.append('<div class="lines">' + "<br>".join(_link(l) for l in lines) + "</div>")
    h.append("</article>")
    return Markup("".join(h))


def page_html(title: str, body: Markup) -> str:
    """A standalone page for the print route: the document on a grey desk on screen, on its own when printed."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"robots\" content=\"noindex\"><title>{escape(title)}</title><style>{DOC_CSS}"
            "body{margin:0;background:#e5e7eb}.bar{font-family:system-ui,sans-serif;padding:12px 16px;font-size:14px;color:#374151;text-align:center}"
            ".bar button{font:inherit;padding:6px 14px;margin-right:10px}.sheet{box-shadow:0 2px 14px rgba(0,0,0,.18);margin:0 auto 24px;max-width:8.27in}"
            "@media print{body{background:#fff}.bar{display:none}.sheet{box-shadow:none;margin:0;max-width:none}}</style></head><body>"
            "<div class=\"bar\"><button onclick=\"window.print()\">Print or save as PDF</button>Choose &ldquo;Save as PDF&rdquo; as the destination to get a file.</div>"
            f"<div class=\"sheet\">{body}</div></body></html>")


# ------------------------------------------------------------------ plain text
_WIDTH = 78


def _wrap(text: str, indent: str = "", sub: str | None = None) -> list[str]:
    return textwrap.wrap(text, width=_WIDTH, initial_indent=indent, subsequent_indent=indent if sub is None else sub,
                         break_long_words=False, break_on_hyphens=False) or [indent.rstrip()]


def _pack(items: list[str], sep: str = "  |  ") -> list[str]:
    """Contact items on as few lines as fit, breaking only between items so a separator never ends or starts a line."""
    lines, cur = [], ""
    for it in items:
        if cur and len(cur) + len(sep) + len(it) > _WIDTH:
            lines.append(cur)
            cur = it
        else:
            cur = f"{cur}{sep}{it}" if cur else it
    return lines + ([cur] if cur else [])


# How each template lays out the .txt: plain text has no fonts or colours, so the three differ in structure.
_TXT = {
    "classic": dict(heading=lambda t: [t, "-" * len(t)], bullet=("  - ", "    "), para_gap=True, entry_gap=True, one_line=False, name=str.upper),
    "modern": dict(heading=lambda t: [f"== {t} =="], bullet=("  * ", "    "), para_gap=True, entry_gap=True, one_line=False, name=str.upper),
    "compact": dict(heading=lambda t: [t], bullet=("- ", "  "), para_gap=False, entry_gap=False, one_line=True, name=str.upper),
}


def resume_txt(text: str, template: str = DEFAULT_TEMPLATE) -> str:
    cfg = _TXT[template_name(template)]
    r = parse_resume(text)
    out: list[str] = []
    if r.name:
        out.append(cfg["name"](r.name))
    if r.contact:
        out += _pack(r.contact)
    first, rest = cfg["bullet"]
    for sec in r.sections:
        out.append("")
        if sec.title:
            out += cfg["heading"](sec.title)
        last_kind = ""
        for kind, idx in sec.order:
            if kind == "e" and last_kind == "e" and cfg["entry_gap"]:
                out.append("")
            last_kind = kind
            if kind == "p":
                out += _wrap(sec.paragraphs[idx]) + ([""] if cfg["para_gap"] else [])
            elif kind == "l":
                label, rest_text = sec.labelled[idx]
                out += _wrap(f"{label}: {rest_text}", "", "  ")
            elif kind == "b":
                out += _wrap(sec.bullets[idx], first, rest)
            elif kind == "e":
                e = sec.entries[idx]
                if cfg["one_line"]:
                    line = ", ".join(x for x in (e.title, e.org) if x) + (f"   [{e.dates}]" if e.dates else "")
                    out += _wrap(line) if line.strip() else []
                else:
                    head = e.title + (f"   [{e.dates}]" if e.dates and not e.org else "")
                    out += _wrap(head) if head.strip() else []
                    if e.org:
                        out += _wrap(e.org + (f"   [{e.dates}]" if e.dates else ""))
                for m in e.meta:
                    out += _wrap(m)
                for bl in e.bullets:
                    out += _wrap(bl, first, rest)
        while out and out[-1] == "":
            out.pop()
    return "\r\n".join(out).strip() + "\r\n"


def letter_txt(text: str, head: tuple[str, list[str]] | None = None, template: str = DEFAULT_TEMPLATE) -> str:
    cfg = _TXT[template_name(template)]
    L = parse_letter(text)
    out: list[str] = []
    if head and head[0]:
        out.append(head[0])
        if head[1]:
            out += _pack(head[1])
        if template_name(template) == "modern":
            out.append("=" * min(_WIDTH, max(len(head[0]), 20)))
        out.append("")
    for lines in L.paragraphs:
        if len(lines) == 1:
            out += _wrap(lines[0])
        else:
            out += lines
        out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\r\n".join(out) + "\r\n"


# ------------------------------------------------------------------ PDF
_BUILTIN = ("Helvetica", "Times-Roman")


def _fonts(text: str, serif: bool = False) -> tuple[str, str, str]:
    """Helvetica (Times for a serif template) when every character is in Western European encoding; otherwise a Unicode font from the system if there is one."""
    try:
        text.encode("cp1252")
        return ("Times-Roman", "Times-Bold", "Times-Italic") if serif else ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique")
    except UnicodeEncodeError:
        pass
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [
        ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "C:/Windows/Fonts"]),
        ("arial.ttf", "arialbd.ttf", "ariali.ttf", ["C:/Windows/Fonts", "/Library/Fonts", "/System/Library/Fonts/Supplemental"]),
    ]
    for reg, bold, ital, dirs in candidates:
        for d in dirs:
            if all(os.path.exists(os.path.join(d, f)) for f in (reg, bold, ital)):
                for name, f in (("CS-Regular", reg), ("CS-Bold", bold), ("CS-Italic", ital)):
                    if name not in pdfmetrics.getRegisteredFontNames():
                        pdfmetrics.registerFont(TTFont(name, os.path.join(d, f)))
                return "CS-Regular", "CS-Bold", "CS-Italic"
    return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"


def _fit(s: str, fonts: tuple[str, str, str]) -> str:
    if fonts[0] in _BUILTIN:
        return s.encode("cp1252", "replace").decode("cp1252")
    return s


def _para_markup(s: str, fonts, link_color="#1d4ed8") -> str:
    """Paragraph markup: escaped, with clickable web and e-mail addresses."""
    s = _fit(s, fonts)
    out, pos = [], 0
    urls = [(m.start(), m.end(), m.group(0), "u") for m in _URL.finditer(s)]
    mails = [(m.start(), m.end(), m.group(0), "e") for m in _EMAILRE.finditer(s) if not any(a <= m.start() < b for a, b, _, _ in urls)]
    for a, b, v, kind in sorted(urls + mails):
        if a < pos:
            continue
        out.append(escape(s[pos:a], quote=False))
        href = v if kind == "u" else "mailto:" + v
        out.append(f'<a href="{escape(href)}" color="{link_color}">{escape(v, quote=False)}</a>')
        pos = b
    out.append(escape(s[pos:], quote=False))
    return "".join(out)


def _pack_pdf(items: list[str], f, font: str, size: float, width: float) -> str:
    """Contact markup with <br/> between lines, breaking only between items."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    sep = "  |  "
    lines, cur = [], []
    for it in items:
        trial = sep.join(cur + [it])
        if cur and stringWidth(_fit(trial, f), font, size) > width:
            lines.append(cur)
            cur = [it]
        else:
            cur.append(it)
    if cur:
        lines.append(cur)
    return "<br/>".join(sep.join(_para_markup(i, f) for i in ln) for ln in lines)


def _doc(buf, paper: str, margins):
    from reportlab.lib.pagesizes import A4, LETTER
    from reportlab.platypus import SimpleDocTemplate
    l, r, t, b = margins
    return SimpleDocTemplate(buf, pagesize=LETTER if paper == "letter" else A4, leftMargin=l, rightMargin=r, topMargin=t, bottomMargin=b,
                             title="", author="")


# Per template: the typeface family, sizes (points), spacing and margins (inches). Classic keeps the original single look.
_PDF = {
    "classic": dict(serif=False, accent="#111111", name_size=21, name_align=1, head_size=10.3, head_color="#111111", rule="#6b7280", rule_w=0.7,
                    body=10, lead=14, para_gap=5, bullet_gap=2.4, entry_gap=6, head_before=11, margins=(0.8, 0.8, 0.7, 0.7),
                    letter=dict(name_size=17, body=10.8, lead=16.2, gap=11, rule_after=22, rule_w=0.7, rule="#6b7280", margins=(1.0, 1.0, 0.9, 0.9))),
    "modern": dict(serif=False, accent="#1e3a8a", name_size=23, name_align=0, head_size=10.3, head_color="#1e3a8a", rule=None, rule_w=0,
                   body=10, lead=14, para_gap=5, bullet_gap=2.4, entry_gap=7, head_before=14, margins=(0.8, 0.8, 0.7, 0.7),
                   letter=dict(name_size=19, body=10.8, lead=16.2, gap=11, rule_after=22, rule_w=1.6, rule="#1e3a8a", margins=(1.0, 1.0, 0.9, 0.9))),
    "compact": dict(serif=True, accent="#111111", name_size=18, name_align=1, head_size=9.6, head_color="#111111", rule="#111111", rule_w=0.6,
                    body=9.6, lead=12.6, para_gap=3.5, bullet_gap=1.2, entry_gap=3.5, head_before=8, margins=(0.65, 0.65, 0.55, 0.55),
                    letter=dict(name_size=15, body=10.4, lead=14.6, gap=9, rule_after=14, rule_w=0.6, rule="#111111", margins=(0.9, 0.9, 0.8, 0.8))),
}


def resume_pdf(text: str, paper: str = "a4", title: str = "", template: str = DEFAULT_TEMPLATE) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, Spacer, Table, TableStyle

    t = _PDF[template_name(template)]
    r = parse_resume(text)
    f = _fonts(text, t["serif"])
    reg, bold, ital = f
    b, lead = t["body"], t["lead"]
    ink, grey = colors.HexColor("#1a1a1a"), colors.HexColor("#4b5563")
    name_s = ParagraphStyle("name", fontName=bold, fontSize=t["name_size"], leading=t["name_size"] + 4, alignment=t["name_align"],
                            textColor=colors.HexColor(t["accent"]), spaceAfter=3)
    contact_s = ParagraphStyle("contact", fontName=reg, fontSize=b - 0.8, leading=b + 2.5, alignment=t["name_align"], textColor=grey, spaceAfter=6)
    head_s = ParagraphStyle("head", fontName=bold, fontSize=t["head_size"], leading=t["head_size"] + 2.7, textColor=colors.HexColor(t["head_color"]),
                            spaceBefore=t["head_before"], spaceAfter=2 if t["rule"] else 5)
    body_s = ParagraphStyle("body", fontName=reg, fontSize=b, leading=lead, textColor=ink, spaceAfter=t["para_gap"])
    title_s = ParagraphStyle("title", parent=body_s, fontName=bold, spaceAfter=0, leading=lead - 0.5)
    dates_s = ParagraphStyle("dates", parent=body_s, fontSize=b - 0.7, textColor=grey, alignment=TA_RIGHT, spaceAfter=0, leading=lead - 0.5)
    org_s = ParagraphStyle("org", parent=body_s, fontName=ital, textColor=colors.HexColor("#333333"), spaceAfter=1, leading=lead - 0.5)
    meta_s = ParagraphStyle("meta", parent=body_s, fontSize=b - 0.7, textColor=grey, spaceAfter=1, leading=b + 2.5)
    bul_s = ParagraphStyle("bul", parent=body_s, leftIndent=14, bulletIndent=3, spaceAfter=t["bullet_gap"], leading=lead - 0.4, bulletFontName=reg)
    buf = io.BytesIO()
    doc = _doc(buf, paper, tuple(x * inch for x in t["margins"]))
    flow: list = []
    if r.name:
        flow.append(Paragraph(_para_markup(r.name, f), name_s))
    if r.contact:
        flow.append(Paragraph(_pack_pdf(r.contact, f, reg, b - 0.8, doc.width - 12), contact_s))
    for sec in r.sections:
        if sec.title:
            flow.append(Paragraph(_para_markup(sec.title, f), head_s))
            if t["rule"]:
                flow.append(HRFlowable(width="100%", thickness=t["rule_w"], color=colors.HexColor(t["rule"]), spaceBefore=0, spaceAfter=5))
        for kind, idx in sec.order:
            if kind == "p":
                flow.append(Paragraph(_para_markup(sec.paragraphs[idx], f), body_s))
            elif kind == "l":
                label, rest = sec.labelled[idx]
                flow.append(Paragraph(f"<b>{_para_markup(label, f)}:</b> {_para_markup(rest, f)}", body_s))
            elif kind == "b":
                flow.append(Paragraph(_para_markup(sec.bullets[idx], f), bul_s, bulletText="•"))
            elif kind == "e":
                e = sec.entries[idx]
                block: list = []
                if e.title or e.dates:
                    row = Table([[Paragraph(_para_markup(e.title, f), title_s), Paragraph(_para_markup(e.dates, f), dates_s)]],
                                colWidths=[doc.width - 12 - 1.55 * inch, 1.55 * inch], hAlign="LEFT")
                    row.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                             ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
                    block.append(row)
                if e.org:
                    block.append(Paragraph(_para_markup(e.org, f), org_s))
                for mt in e.meta:
                    block.append(Paragraph(_para_markup(mt, f), meta_s))
                bullets = [Paragraph(_para_markup(bl, f), bul_s, bulletText="•") for bl in e.bullets]
                if bullets:
                    flow.append(KeepTogether(block + bullets[:1]))      # a role's heading never sits alone at the foot of a page
                    flow += bullets[1:]
                else:
                    flow.append(KeepTogether(block))
                flow.append(Spacer(1, t["entry_gap"]))
    doc.title = title or r.name or "Résumé"
    doc.author = r.name or ""
    doc.build(flow or [Paragraph("", body_s)])
    return buf.getvalue()


def letter_pdf(text: str, head: tuple[str, list[str]] | None = None, paper: str = "a4", title: str = "", template: str = DEFAULT_TEMPLATE) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph

    t = _PDF[template_name(template)]
    lt = t["letter"]
    L = parse_letter(text)
    f = _fonts(text + (head[0] if head else "") + " ".join(head[1] if head else []), t["serif"])
    reg, bold, _ = f
    body_s = ParagraphStyle("lbody", fontName=reg, fontSize=lt["body"], leading=lt["lead"], textColor=colors.HexColor("#1a1a1a"), spaceAfter=lt["gap"])
    name_s = ParagraphStyle("lname", fontName=bold, fontSize=lt["name_size"], leading=lt["name_size"] + 4, textColor=colors.HexColor(t["accent"]), spaceAfter=2)
    contact_s = ParagraphStyle("lcontact", fontName=reg, fontSize=9.2, leading=12.5, textColor=colors.HexColor("#4b5563"), spaceAfter=4)
    buf = io.BytesIO()
    doc = _doc(buf, paper, tuple(x * inch for x in lt["margins"]))
    flow: list = []
    if head and head[0]:
        flow.append(Paragraph(_para_markup(head[0], f), name_s))
        if head[1]:
            flow.append(Paragraph(_pack_pdf(head[1], f, reg, 9.2, doc.width - 12), contact_s))
        flow.append(HRFlowable(width="100%", thickness=lt["rule_w"], color=colors.HexColor(lt["rule"]), spaceBefore=2, spaceAfter=lt["rule_after"]))
    for lines in L.paragraphs:
        if len(lines) == 1:
            flow.append(Paragraph(_para_markup(lines[0], f), body_s))
        else:
            flow.append(KeepTogether([Paragraph("<br/>".join(_para_markup(l, f) for l in lines), body_s)]))
    doc.title = title or "Cover letter"
    doc.author = (head[0] if head else "") or ""
    doc.build(flow or [Paragraph("", body_s)])
    return buf.getvalue()
