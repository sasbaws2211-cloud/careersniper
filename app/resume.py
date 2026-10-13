"""A member's résumé as plain text, kept for the AI tools (job match now; cover letters and interview practice later).

One résumé per member. Pasting new text replaces the old one and throws away everything derived from it.
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Member, Resume

MIN_CHARS = 200          # shorter than this is a heading, not a résumé
MAX_CHARS = 15000        # roughly six pages
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ResumeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def clean(text: str) -> str:
    t = _CONTROL.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    return re.sub(r"\n{4,}", "\n\n\n", t).strip()


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


async def get_main(db: AsyncSession, member: Member) -> Resume | None:
    return await db.scalar(select(Resume).where(Resume.member_id == member.id).order_by(Resume.updated_at.desc(), Resume.id.desc()))


def text_of(r: Resume | None) -> str:
    return ((r.data or {}).get("text") or "") if r else ""


async def save_text(db: AsyncSession, member: Member, text: str) -> Resume:
    text = clean(text)
    if len(text) < MIN_CHARS:
        raise ResumeError(f"That is too short to be a résumé. Paste the full text (at least {MIN_CHARS} characters).")
    if len(text) > MAX_CHARS:
        raise ResumeError(f"That is too long. The limit is {MAX_CHARS:,} characters (about six pages); you pasted {len(text):,}. Trim the oldest roles.")
    r = await get_main(db, member)
    h = digest(text)
    if r and (r.data or {}).get("hash") == h:
        return r                                    # the same text again: keep the profile and the last match
    data = {"text": text, "hash": h}                # new text: any profile or match made from the old text is stale
    if r:
        r.data = data
    else:
        r = Resume(member_id=member.id, title="My résumé", data=data)
        db.add(r)
    await db.commit()
    return r


async def delete(db: AsyncSession, member: Member) -> None:
    for r in (await db.scalars(select(Resume).where(Resume.member_id == member.id))):
        await db.delete(r)
    await db.commit()
