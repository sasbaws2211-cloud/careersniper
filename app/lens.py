"""The "matching my résumé" view of the board.

Built from the profile that was read off the member's résumé (job titles to look for, seniority). It is a plain WHERE clause on the
job title and seniority columns, so browsing the board through it costs no AI tokens. It is a view, not a wall: the member can
switch it off with one click, and it is paused while they search for something specific.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from sqlalchemy import and_, func, or_

from .match import STOP
from .models import Job

# profile seniority -> the stored job seniority values that are a reasonable fit ("" = a title that names no level)
BANDS = {
    "junior": ("intern", "entry", ""),
    "mid": ("entry", "", "senior"),
    "senior": ("senior", "lead", "manager", ""),
    "lead": ("lead", "senior", "manager", "director", ""),
}
SENIORITY_LABEL = {"junior": "Early career", "mid": "Mid-level", "senior": "Senior", "lead": "Lead / principal"}
MAX_TITLES = 6

# words employers use interchangeably in a title: a résumé that says "Software Engineer" should also find "Software Developer"
SYNONYMS = {
    "engineer": ("developer", "programmer"), "developer": ("engineer", "programmer"), "programmer": ("engineer", "developer"),
    "accountant": ("accounting",), "designer": ("design",), "analyst": ("analytics",), "marketer": ("marketing",),
    "recruiter": ("talent",), "writer": ("copywriter", "editor"), "scientist": ("science",), "administrator": ("admin",),
    "frontend": ("front-end", "front"), "backend": ("back-end", "back"), "fullstack": ("full-stack", "full"),
    "devops": ("sre", "platform"), "qa": ("tester", "test"), "ux": ("product designer",), "hr": ("people",),
}


class Lens(NamedTuple):
    titles: tuple[str, ...]
    seniority: str = ""
    skills: tuple[str, ...] = ()
    years: int = 0

    @classmethod
    def from_profile(cls, profile) -> "Lens | None":
        if not isinstance(profile, dict):
            return None
        titles = tuple(t for t in (profile.get("titles") or []) if isinstance(t, str) and t.strip())[:MAX_TITLES]
        if not titles or not any(cls._words(t) for t in titles):
            return None
        sen = profile.get("seniority") if profile.get("seniority") in BANDS else ""
        try:
            years = int(profile.get("years") or 0)
        except (TypeError, ValueError):
            years = 0
        return cls(titles, sen, tuple(s for s in (profile.get("skills") or []) if isinstance(s, str))[:15], years)

    @staticmethod
    def _words(title: str) -> list[str]:
        out: list[str] = []
        for w in re.split(r"[^a-z0-9+#]+", title.lower()):
            if len(w) >= 3 and w not in STOP and w not in out:
                out.append(w)
        return out

    def apply(self, q):
        """Keep jobs whose title contains every meaningful word of at least one of the member's titles, at a level that fits."""
        groups = [self._words(t) for t in self.titles]
        def word(w):
            alts = (w,) + SYNONYMS.get(w, ())
            return or_(*[func.lower(Job.title).contains(x) for x in alts]) if len(alts) > 1 else func.lower(Job.title).contains(w)
        conds = [and_(*[word(w) for w in g]) for g in groups if g]
        if conds:
            q = q.where(or_(*conds))
        band = BANDS.get(self.seniority)
        if band:
            named = [b for b in band if b]
            level = [Job.seniority.in_(named)] if named else []
            if "" in band:
                level += [Job.seniority.is_(None), Job.seniority == ""]
            q = q.where(or_(*level))
        return q

    def why(self, job_title: str) -> str:
        """The résumé title this job matched, for the 'why am I seeing this' chip. '' when none is evident."""
        low = (job_title or "").lower()
        for t in self.titles:
            words = self._words(t)
            if words and all(any(x in low for x in (w,) + SYNONYMS.get(w, ())) for w in words):
                return t.title()
        return ""

    @property
    def level_label(self) -> str:
        return SENIORITY_LABEL.get(self.seniority, "")

    @property
    def summary(self) -> str:
        bits = [t.title() for t in self.titles[:3]]
        if self.level_label:
            bits.append(self.level_label)
        return " · ".join(bits)
