"""A source tag on the apply links we send people to, so the employer can see in their own hiring system that an applicant came from CareerSniper.

Greenhouse reads `gh_src`, Lever reads `lever-source`, and everything else gets the standard `utm_source`. The tag is added when someone clicks Apply:
the stored job address is never changed. Nothing else about the address is touched (path, existing parameters, fragment), an address that already
carries the tag is left alone, and anything that is not a web address (a mailto: link) is returned as it was.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit, urlunsplit

SOURCE = "careersniper"
_PARAM = {"greenhouse": "gh_src", "lever": "lever-source"}
DEFAULT_PARAM = "utm_source"


def param_for(ats: str | None) -> str:
    return _PARAM.get((ats or "").strip().lower(), DEFAULT_PARAM)


def tag(url: str | None, ats: str | None = None) -> str:
    """`url` with the source tag added for the hiring system `ats`."""
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return u
    parts = urlsplit(u)
    name = param_for(ats)
    if any(k.lower() == name for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return u                                        # the address already names a source: keep theirs
    query = f"{parts.query}&{name}={SOURCE}" if parts.query else f"{name}={SOURCE}"   # appended as text, so existing parameters stay byte for byte
    return urlunsplit(parts._replace(query=query))


def for_job(job) -> str:
    """The address to send an applicant to for `job`: its own address plus the tag that fits the system it came from."""
    ats = "" if getattr(job, "source", None) == "direct" else (getattr(getattr(job, "company", None), "ats", "") or "")
    return tag(getattr(job, "url", ""), ats)
