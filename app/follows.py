"""Following employers: a member follows companies and gets one weekly email of the new roles they opened.

A follow is stored against the company's canonical record (the lowest id among records with the same name), and looked up
by name when the digest runs, so an employer that is on two job boards counts as one. The digest goes to members whose email
is confirmed, only when there is something new, at most once a week, and is never sent twice for the same jobs: the window
(`last_checked_at`) moves forward only after the email was accepted. Unsubscribing is one click and needs no login.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import companies, config, dedupe, mailer
from .models import Company, CompanyFollow, FollowDigest, Job, Member

MAX_FOLLOWS = 100
DUE = timedelta(days=6, hours=23)
PER_COMPANY = 8
PER_DIGEST = 40
_signer = None


class FollowError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _serializer() -> URLSafeSerializer:
    global _signer
    if _signer is None or _signer.secret_keys[0] != config.SECRET_KEY.encode():
        _signer = URLSafeSerializer(config.SECRET_KEY, salt="follow-unsubscribe")
    return _signer


def unsubscribe_token(member: Member) -> str:
    return _serializer().dumps({"m": member.id})


async def member_for_token(db: AsyncSession, token: str) -> Member | None:
    try:
        data = _serializer().loads(token)
    except BadSignature:
        return None
    return (await db.get(Member, data.get("m"))) if isinstance(data, dict) else None


# ---------------------------------------------------------------- following
async def _pref(db: AsyncSession, member: Member, now: datetime | None = None) -> FollowDigest:
    p = await db.scalar(select(FollowDigest).where(FollowDigest.member_id == member.id))
    if not p:
        p = FollowDigest(member_id=member.id, enabled=True, last_checked_at=now or datetime.utcnow())
        db.add(p)
        await db.flush()
    return p


async def follow(db: AsyncSession, member: Member, company: Company) -> CompanyFollow:
    """Follow a company (any of its board records). Following twice does nothing."""
    target = await companies.canonical(db, company)
    have = await db.scalar(select(CompanyFollow).where(CompanyFollow.member_id == member.id, CompanyFollow.company_id == target.id))
    if have:
        return have
    n = (await db.scalar(select(func.count()).select_from(CompanyFollow).where(CompanyFollow.member_id == member.id))) or 0
    if n >= MAX_FOLLOWS:
        raise FollowError(f"You follow {MAX_FOLLOWS} companies, the most we allow. Unfollow some first.")
    row = CompanyFollow(member_id=member.id, company_id=target.id)
    db.add(row)
    await _pref(db, member)
    await db.commit()
    return row


async def unfollow(db: AsyncSession, member: Member, company: Company) -> None:
    target = await companies.canonical(db, company)
    for row in (await db.scalars(select(CompanyFollow).where(CompanyFollow.member_id == member.id, CompanyFollow.company_id.in_([target.id, company.id])))):
        await db.delete(row)
    await db.commit()


async def is_following(db: AsyncSession, member: Member | None, company: Company) -> bool:
    if not member:
        return False
    target = await companies.canonical(db, company)
    return (await db.scalar(select(CompanyFollow.id).where(CompanyFollow.member_id == member.id, CompanyFollow.company_id.in_([target.id, company.id])))) is not None


async def set_enabled(db: AsyncSession, member: Member, enabled: bool) -> None:
    (await _pref(db, member)).enabled = enabled
    await db.commit()


async def digest_enabled(db: AsyncSession, member: Member) -> bool:
    p = await db.scalar(select(FollowDigest).where(FollowDigest.member_id == member.id))
    return True if p is None else bool(p.enabled)


async def followed(db: AsyncSession, member: Member) -> list[dict]:
    """[{company, open}] for the companies this member follows, most open roles first."""
    rows = (await db.execute(select(Company).join(CompanyFollow, CompanyFollow.company_id == Company.id)
                      .where(CompanyFollow.member_id == member.id))).scalars().all()
    by_id = {r["id"]: r["open"] for r in (await companies._directory_rows(db))}
    out = [{"company": c, "open": by_id.get(c.id, 0), "path": companies.path(c)} for c in rows if c.active]
    return sorted(out, key=lambda d: (-d["open"], d["company"].name.lower()))


# ---------------------------------------------------------------- what is new
async def new_roles(db: AsyncSession, member: Member, since: datetime, until: datetime) -> dict[str, list[dict]]:
    """{company name: [role, ...]} for live roles first seen in (since, until] at the companies the member follows, newest first."""
    from .main import feed_query
    names = {c.name.lower(): c.name for c in (await db.execute(select(Company).join(CompanyFollow, CompanyFollow.company_id == Company.id)
                                                       .where(CompanyFollow.member_id == member.id))).scalars()}
    if not names:
        return {}
    id_to_name = {c.id: names[c.name.lower()] for c in (await db.scalars(select(Company).where(func.lower(Company.name).in_(list(names)), Company.active.is_(True))))}
    if not id_to_name:
        return {}
    q = (feed_query().where(Job.company_id.in_(list(id_to_name)), Job.first_seen > since, Job.first_seen <= until)
         .with_only_columns(Job.id, Job.title, Job.location, Job.company_id, Job.dup_key, Job.first_seen).order_by(Job.first_seen.desc(), Job.id.desc()))
    looks = await companies.first_looks(db, list(id_to_name))
    out: dict[str, list[dict]] = {}
    seen: set = set()
    for r in (await db.execute(q)):
        if not companies.is_new(r.first_seen, looks.get(r.company_id)):
            continue                    # already on the board when we first looked: not news (else a newly added company floods the email)
        name = id_to_name[r.company_id]
        key = (name, r.dup_key or f"id:{r.id}")
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(name, []).append({"id": r.id, "title": r.title, "location": r.location or "", "first_seen": r.first_seen})
    return out


async def recent(db: AsyncSession, member: Member, days: int = 7, now: datetime | None = None) -> dict[str, list[dict]]:
    now = now or datetime.utcnow()
    return await new_roles(db, member, now - timedelta(days=days), now)


# ---------------------------------------------------------------- the weekly email
def _message(member: Member, groups: dict[str, list[dict]]):
    base = config.PUBLIC_URL.rstrip("/")
    unsub = f"{base}/following/unsubscribe/{unsubscribe_token(member)}"
    groups = dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0].lower())))       # the company with most news first
    total = sum(len(v) for v in groups.values())
    names = list(groups)
    subject = f"{total} new role{'s' if total != 1 else ''} at {names[0]}" + (f" and {len(names) - 1} other compan{'ies' if len(names) > 2 else 'y'}" if len(names) > 1 else "")
    lines, html = [], []
    shown = 0
    for name, roles in groups.items():
        lines.append(f"\n{name}")
        html.append(f'<p style="margin:18px 0 6px;font-weight:700">{escape(name)}</p>')
        for r in roles[:PER_COMPANY]:
            if shown >= PER_DIGEST:
                break
            shown += 1
            where = f" · {r['location']}" if r["location"] else ""
            link = f"{base}/jobs/{r['id']}"
            lines.append(f"- {r['title']}{where}\n  {link}")
            html.append(f'<p style="margin:0 0 8px"><a href="{link}" style="color:#2563eb;text-decoration:none;font-weight:600">{escape(r["title"])}</a>'
                        f'<span style="color:#555">{escape(where)}</span></p>')
        extra = len(roles) - min(len(roles), PER_COMPANY)
        if extra > 0:
            lines.append(f"  ...and {extra} more at {name}")
            html.append(f'<p style="margin:0 0 8px;color:#555">...and {extra} more at {escape(name)}</p>')
    text = ("New roles at companies you follow:\n" + "\n".join(lines) + f"\n\nSee who you follow: {base}/following\nStop these emails: {unsub}")
    body = ("<p style='color:#555'>New roles at companies you follow:</p>" + "".join(html) + mailer.button(f"{base}/following", "See everything you follow")
            + f'<p style="color:#888;font-size:12px"><a href="{unsub}">Stop these weekly emails</a></p>')
    headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return subject, text, mailer.wrap_html(body), headers


async def run_digests(db: AsyncSession, now: datetime | None = None) -> dict:
    """Send the weekly follow digests that are due. The window advances only after the email was accepted (or there was nothing to say)."""
    now = now or datetime.utcnow()
    stats = {"checked": 0, "sent": 0, "failed": 0}
    prefs = (await db.scalars(select(FollowDigest).where(FollowDigest.enabled.is_(True)))).all()
    for p in prefs:
        since = p.last_checked_at or now - timedelta(days=7)
        if now - since < DUE:
            continue
        member = await db.get(Member, p.member_id)
        if not member or not member.verified:
            continue                                    # an unconfirmed address may be a typo: wait until it is confirmed
        stats["checked"] += 1
        groups = await new_roles(db, member, since, now)
        if groups:
            try:
                subject, text, html, headers = _message(member, groups)
                await mailer.send(member.email, subject, text, html, from_name=config.PLATFORM_NAME, headers=headers)
            except mailer.MailError:
                stats["failed"] += 1
                continue
            p.last_sent_at, p.digests_sent = now, (p.digests_sent or 0) + 1
            stats["sent"] += 1
        p.last_checked_at = now
        await db.commit()
    return stats
