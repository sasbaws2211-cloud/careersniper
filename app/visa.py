"""Does the employer say it sponsors work visas? Read from the job text, one clause at a time.

Answers 'yes', 'no', or '' (silent, unclear, or contradicting itself). A wrong 'yes' sends someone applying for a job that will
not sponsor them and a wrong 'no' hides a job that would, so anything unclear is left as ''.

Descriptions arrive as one flat paragraph with sentences glued together, so this works on the clause around each mention of
'sponsor' rather than on pre-split sentences.
"""
import html
import re

_SPONSOR = re.compile(r"\bsponsor(?:s|ed|ing|ship|ships)?\b", re.I)
_BOUNDARY = re.compile(r"[.!?;]\s|\s[•●·|]\s|\n|[☀-➿\U0001f300-\U0001faff]")   # sentence ends, bullets, emoji bullets
_ABBREV = re.compile(r"\bU\.\s?S\.(?:\s?A\.)?|\bU\.\s?K\.|\be\.g\.|\bi\.e\.|\betc\.|\bInc\.|\bLtd\.|\bCo\.", re.I)
# the clause is about immigration status, not a sponsored event or an executive sponsor
_VISA_CONTEXT = re.compile(
    r"\bvisas?\b|\bh-?1-?b\b|\bh-?1\b|\bwork permit|\bwork (?:authori[sz]ation|eligibility|status)|\bauthori[sz]ed to work|\beligible to work|"
    r"\bright to work|\blegally (?:authori[sz]ed|permitted)|\bimmigration|\bemployment (?:authori[sz]ation|eligibility|visa)|\bwork in the (?:us|united states)|"
    r"\bsponsorship (?:is|are|will|can|may|for (?:this|the|any) (?:role|position|job))\b|"
    r"\b(?:no|not|without|unable)\b[^.]{0,25}\bsponsorship\b|\bsponsorship\b[^.]{0,25}\b(?:no|not|available|provided|offered|unavailable)\b|"
    r"\brelocation\b[^.]{0,40}\bsponsor", re.I)
_NEGATION = re.compile(r"\b(?:not|no|never|unable|cannot|can't|cant|without|ineligible|isn't|aren't|doesn't|don't|won't|wouldn't|"
                       r"couldn't|neither|nor)\b|n't\b", re.I)
_POSITIVE = re.compile(r"\b(?:available|offer(?:s|ed|ing)?|provid(?:e|es|ed|ing)|support(?:s|ed|ing)?|will|can|may|do|does|are able|is able|"
                       r"able to|happy|glad|open to|willing|eligible|yes|include[sd]?|assist(?:ance)?|cover(?:s|ed)?|take over|transfer|"
                       r"where applicable|for the right|we sponsor|sponsors? (?:work |employment )?visas?)\b", re.I)
# "we aren't able to sponsor for every role": a limit on a promise, not a refusal
_HEDGE = re.compile(r"\b(?:every|all|each|guarantee[sd]?|always)\b", re.I)
# a clause that asks or conditions rather than states ("Do you require sponsorship?", "Applicants who require sponsorship must ...")
_ASKING = re.compile(r"\?|\b(?:do|will|would)\s+you\b|\bif\s+you\b|\bwho\s+(?:require|need)s?\b|\bapplicants?\s+(?:requiring|needing)\b|"
                     r"\bcandidates?\s+(?:requiring|needing)\b|\bthose\s+(?:requiring|needing)\b", re.I)
_NOT_VISA = re.compile(r"\bexport\b|\bsponsor(?:ed|ing)?\s+(?:events?|programs?|content|teams?|meals?|conferences?|licen[sc]e)\b|"
                       r"\b(?:company|co)[- ]sponsored\b|\bexecutive\s+sponsor|\bprocess\s+sponsored\b|\bsponsored\s+by\b", re.I)
# the candidate must already be free to work, which also means the employer will not sponsor
_MUST_NOT_NEED = re.compile(r"\b(?:not|never)\s+(?:now\s+or\s+in\s+the\s+future\s+)?(?:require|need)s?\s+(?:\w+\s+){0,3}sponsorship|"
                            r"\bwithout\s+(?:the\s+need\s+for\s+)?(?:\w+\s+){0,3}sponsorship|\bwithout\s+(?:requiring|needing)\b", re.I)
_THIS_ROLE = re.compile(r"\b(?:this|the)\s+(?:role|position|job|opening|vacancy)\b", re.I)
REACH = 260       # characters either side of a mention that can belong to its clause


def _clauses(text: str):
    """The clause around each mention of 'sponsor': from the previous sentence break to the next, no more than REACH either way."""
    seen = set()
    for m in _SPONSOR.finditer(text):
        a, b = max(0, m.start() - REACH), min(len(text), m.end() + REACH)
        before, after = text[a:m.start()], text[m.end():b]
        breaks = list(_BOUNDARY.finditer(before))
        start = breaks[-1].end() if breaks else 0
        nxt = _BOUNDARY.search(after)
        end = nxt.start() + 1 if nxt else len(after)
        clause = (before[start:] + m.group(0) + after[:end]).strip()
        key = (a + start, len(clause))
        if key not in seen and len(clause) >= 12:
            seen.add(key)
            yield clause, len(before) - start, len(m.group(0))


def visa_of(text: str | None) -> str:
    """'yes' | 'no' | '' for the work-visa sponsorship statements in a job description."""
    if not text or "sponsor" not in text.lower():
        return ""
    text = _ABBREV.sub(lambda m: re.sub(r"[\s.]", "", m.group(0)).upper() if m.group(0)[:1] in "Uu" else m.group(0).replace(".", ""),
                       html.unescape(html.unescape(text)).replace("\xa0", " "))
    yes = no = no_for_this_role = False
    for clause, pos, length in _clauses(text):
        if _NOT_VISA.search(clause) or not _VISA_CONTEXT.search(clause):
            continue
        if _MUST_NOT_NEED.search(clause):
            no = True
            continue
        if _ASKING.search(clause):
            continue
        if _NEGATION.search(clause):
            if _HEDGE.search(clause[pos:pos + length + 45]):      # "sponsor visas for every role": not a refusal
                continue
            no = True
            no_for_this_role = no_for_this_role or bool(_THIS_ROLE.search(clause))
        elif _POSITIVE.search(clause):
            yes = True
    if yes and no and not no_for_this_role:       # a refusal for this very role beats a general statement; other conflicts stay unknown
        return ""
    return "yes" if (yes and not no) else ("no" if no else "")
