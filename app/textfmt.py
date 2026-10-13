"""Job descriptions as readable, structured HTML.

Postings reach us as plain text, and for most boards every line break was lost when the page was scraped, so a description is one
long paragraph. `render` rebuilds the structure that is recoverable: section headings ("About the role", "Requirements"...), bullet
lists and paragraphs of a sensible length. It only ever produces <h3>, <p>, <ul> and <li> from text that has been escaped, so the
result is safe to print as-is.

`structure` is the text-to-text half (also used by the scraper's HTML converter); `render` turns that into HTML.
"""
from __future__ import annotations

import re
from html import escape, unescape as html_unescape

from markupsafe import Markup

# phrases that, on their own, start a section of a posting
_HEADS = (
    r"about (?:the )?(?:role|job|position|opportunity|team|us|you|company|the company|this role|this job)",
    r"(?:the )?(?:role|opportunity|position)",
    r"who we are", r"who you are", r"who we(?:'|’)re looking for", r"what we(?:'|’)re looking for", r"what you(?:'|’)ll need",
    r"what you will need", r"what you bring", r"what you(?:'|’)ll bring", r"what you(?:'|’)ll do", r"what you will do",
    r"what you(?:'|’)ll be doing", r"what you(?:'|’)ll work on", r"what we offer", r"what we(?:'|’)ll offer", r"what(?:'|’)s in it for you",
    r"(?:key |core |main )?responsibilities", r"(?:key )?duties", r"your (?:role|impact|responsibilities|mission)", r"day[- ]to[- ]day",
    r"(?:minimum |basic |required |preferred |desired |additional )?qualifications", r"(?:minimum |basic |required |preferred )?requirements",
    r"(?:required |preferred )?(?:skills|experience)(?: (?:and|&) (?:experience|skills|qualifications))?", r"must[- ]haves?",
    r"nice[- ]to[- ]haves?", r"bonus points?", r"(?:it(?:'|’)s |this is )?a plus", r"preferred", r"ideal candidate",
    r"benefits(?: (?:and|&) perks)?", r"perks(?: (?:and|&) benefits)?", r"compensation(?: (?:and|&) benefits)?", r"salary(?: range)?",
    r"pay range", r"why (?:join|work (?:with|at|for)) us", r"why you(?:'|’)ll love (?:working|it) (?:here|with us)?", r"our (?:mission|values|culture|team|benefits)",
    r"equal (?:opportunity|employment)(?: employer| statement)?", r"how to apply", r"work(?:ing)? (?:location|arrangement|environment|schedule)",
    r"physical (?:requirements|demands)", r"what you need", r"what we need", r"what you have", r"what you get", r"what you(?:'|’)ll get",
    r"(?:cool |exciting |great )?things you(?:'|’)ll do", r"things you(?:'|’)ll (?:work on|own|bring)", r"how you(?:'|’)ll (?:make an )?impact",
    r"you(?:'|’)ll (?:also )?(?:need|have|bring)", r"the (?:team|company|opportunity)",
)
# generic words: a heading only when followed by a colon, so ordinary sentences are never split
_COLON_HEADS = r"location|work location|travel|education|preferred|salary|pay|hours|schedule|visa(?: sponsorship)?|team|department"
_COLON_RE = re.compile(r"(?:^|(?<=[.!?)\]]\s)|(?<=\n))(?P<h>(?:" + _COLON_HEADS + r"))\s*:(?=\s)", re.I)
_LEADIN_RE = re.compile(r"(?<=[.!?]\s)(?P<h>[A-Z][^.!?:\n]{3,90}:)\s+(?=[A-Z0-9])")
# the unambiguous headings may also follow a bullet or an unfinished clause, but only when capitalised, as a heading is written
_MID_HEADS = (r"(?:minimum |basic |required |preferred |desired |additional )?(?:qualifications|requirements)", r"(?:key )?responsibilities",
              r"benefits(?: (?:and|&) perks)?", r"perks(?: (?:and|&) benefits)?", r"what you(?:'|’)ll (?:do|need|bring)", r"what you will (?:do|need)",
              r"what you need", r"what we offer", r"about (?:the )?(?:role|team|job|company)", r"about us", r"nice[- ]to[- ]haves?", r"how to apply")
_MID_RE = re.compile(r"(?<=\s)(?=(?-i:[A-Z]))(?P<h>(?:" + "|".join(_MID_HEADS) + r"))(?=\s*:|\s+(?-i:[A-Z•*\-]))", re.I)
_HEAD_RE = re.compile(r"(?:^|(?<=[.!?:;)\]]\s)|(?<=\n))(?P<h>(?:" + "|".join(_HEADS) + r"))\s*(?::|\n|(?=\s+[A-Z•\-*]))", re.I)
_CAPS_RE = re.compile(r"(?:(?<=[.!?:)]\s)|^)(?P<h>[A-Z][A-Z&/'’,\-]*(?:\s+[A-Z&/'’,\-]+){1,6})(?=\s+[A-Z][a-z]|\s*:|\s*$)")
_BULLET_SPLIT = re.compile(r"\s+[•·▪●◦‣–—-]\s+(?=[A-Z0-9(\"'“])")
_LINE_BULLET = re.compile(r"^\s*(?:[•·▪●◦‣*]|[-–—](?=\s)|\d{1,2}[.)](?=\s))\s*")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(])")
_MAX_PARA = 460


def _is_heading(line: str) -> bool:
    s = line.strip().rstrip(":").strip()
    if not s or len(s) > 70 or s.endswith((".", ",", ";")):
        return False
    if _HEAD_RE.fullmatch(s + ":") or _HEAD_RE.fullmatch(s) is not None:
        return True
    letters = [c for c in s if c.isalpha()]
    return len(s.split()) <= 7 and len(letters) >= 4 and all(c.isupper() for c in letters)


def structure(text: str) -> str:
    """Plain text with real line breaks: headings on their own line, one bullet per line, blank lines between paragraphs."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    if not t.strip():
        return ""
    flat = "\n" not in t.strip()
    if flat:
        t = _CAPS_RE.sub(lambda m: "\n\n" + m.group("h").strip() + "\n", t)
        t = _HEAD_RE.sub(lambda m: "\n\n" + m.group("h").strip() + "\n", t)
        t = _MID_RE.sub(lambda m: "\n\n" + m.group("h").strip() + "\n", t)
        t = _COLON_RE.sub(lambda m: "\n\n" + m.group("h").strip() + "\n", t)
        t = _LEADIN_RE.sub(lambda m: "\n\n" + m.group("h").strip() + "\n", t)
        t = _BULLET_SPLIT.sub("\n• ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _paragraphs(block: str) -> list[str]:
    """One run of prose split into paragraphs of a few sentences each, so no wall of text."""
    if len(block) <= _MAX_PARA:
        return [block]
    out, cur = [], ""
    for sent in _SENT.split(block):
        if cur and len(cur) + len(sent) + 1 > _MAX_PARA:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


def render(text: str, limit: int | None = None) -> Markup:
    """Safe HTML for a posting. `limit` clips the source text first (the free preview), on a word boundary."""
    src = html_unescape(html_unescape(text or ""))     # some boards were stored with entities still in them ("&amp;", "&nbsp;")
    clipped = False
    if limit and len(src) > limit:
        cut = src[:limit]
        src = cut[: cut.rfind(" ")] if " " in cut[-40:] else cut
        clipped = True
    lines = structure(src).split("\n")
    html: list[str] = []
    items: list[str] = []
    para: list[str] = []

    def flush_list():
        nonlocal items
        if items:
            html.append("<ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>")
            items = []

    def flush_para():
        nonlocal para
        if para:
            for chunk in _paragraphs(" ".join(para)):
                html.append(f"<p>{escape(chunk)}</p>")
            para = []

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_para()
            flush_list()
            continue
        m = _LINE_BULLET.match(line)
        if m and line[m.end():].strip():
            flush_para()
            items.append(line[m.end():].strip())
            continue
        if _is_heading(line):
            flush_para()
            flush_list()
            head = line.rstrip(":").strip()
            if head.isupper():
                head = head.capitalize() if len(head.split()) == 1 else head[0] + head[1:].lower()
            html.append(f"<h3>{escape(head)}</h3>")
            continue
        if line.endswith(":") and len(line) <= 95:
            flush_para()
            flush_list()
            html.append(f'<p class="lead-in"><strong>{escape(line)}</strong></p>')
            continue
        flush_list()
        para.append(line)
    flush_para()
    flush_list()
    if clipped and html:
        html.append('<p class="fade-note">&hellip;</p>')
    return Markup("".join(html))
