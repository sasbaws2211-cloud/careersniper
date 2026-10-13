"""Platform settings that an admin can change without a redeploy, kept in the database and read by every process.

The public site and the admin app are separate processes sharing one database, so a setting changed in the admin takes effect on the site
when its short cache expires (TTL seconds, normally ten). A setting that is not stored here falls back to the environment variable it
replaced, so an existing deployment keeps working untouched.

Secrets (the AI provider's API key) are stored encrypted with a key derived from SETTINGS_SECRET, or SECRET_KEY if that is not set. Both
processes must therefore run with the same value. If it differs, the stored secret is unreadable: it is treated as not set and the admin app says so.
They are never shown again after they are saved, only whether one exists and its last four characters.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
from .models import PlatformSetting

log = logging.getLogger("careersniper.settings")
TTL = 10.0
SECRET_KEYS = {"ai_api_key"}
# every key an admin may store, and the type its value must have
KEYS: dict[str, type] = {
    "ai_enabled": bool, "ai_provider": str, "ai_base_url": str, "ai_model": str, "ai_api_key": str, "ai_token_param": str,
    "ai_timeout": float, "ai_max_output_tokens": int, "ai_max_concurrency": int, "ai_json_mode": bool, "ai_free_requests": int,
    "ai_requests_pass": int, "ai_requests_weekly": int, "ai_requests_monthly": int, "ai_requests_lifetime": int,
}
_cache: dict = {"at": 0.0, "values": None, "unreadable": []}


# ---------------------------------------------------------------- encryption
def _fernet() -> Fernet:
    secret = os.getenv("SETTINGS_SECRET") or config.SECRET_KEY
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("careersniper-settings:" + secret).encode()).digest()))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


# ---------------------------------------------------------------- reading
def _coerce(key: str, value):
    """The stored value as the type the setting should have, or None if it is not usable (the setting is then ignored)."""
    want = KEYS[key]
    try:
        if want is bool:
            return value if isinstance(value, bool) else None
        if want is int:
            return None if isinstance(value, bool) else int(value)
        if want is float:
            return None if isinstance(value, bool) else float(value)
        return str(value) if isinstance(value, str) else None
    except (TypeError, ValueError):
        return None


async def load(db: AsyncSession) -> tuple[dict, list[str]]:
    """(values, names of secrets that could not be decrypted). Unknown keys and unusable values are skipped."""
    values, unreadable = {}, []
    for row in (await db.scalars(select(PlatformSetting))):
        if row.key not in KEYS:
            continue
        v = row.value
        if row.key in SECRET_KEYS:
            token = v.get("enc") if isinstance(v, dict) else None
            plain = decrypt(token) if token else None
            if plain is None:
                unreadable.append(row.key)
                continue
            v = plain
        v = _coerce(row.key, v)
        if v is not None:
            values[row.key] = v
    return values, unreadable


async def values() -> dict:
    """The stored settings, cached for TTL seconds. Never raises: if the database cannot be read, the environment is used."""
    now = time.time()
    if _cache["values"] is not None and now - _cache["at"] < TTL:
        return _cache["values"]
    from .db import SessionLocal
    try:
        async with SessionLocal() as db:
            vals, bad = await load(db)
    except Exception:                                  # table missing (not migrated yet) or database down: fall back to the environment
        log.warning("could not read platform settings; using the environment", exc_info=True)
        vals, bad = {}, []
        _cache.update(at=now - TTL + 3, values=vals, unreadable=bad)      # look again in a few seconds
        return vals
    _cache.update(at=now, values=vals, unreadable=bad)
    return vals


def cached_values() -> dict:
    """The last values read, or {} before the first read. Never touches the database."""
    return _cache["values"] or {}


async def get(key: str, default=None):
    return (await values()).get(key, default)


async def unreadable() -> list[str]:
    await values()
    return list(_cache["unreadable"])


def invalidate() -> None:
    _cache.update(at=0.0, values=None, unreadable=[])


# ---------------------------------------------------------------- writing (used by the admin app)
async def save(db: AsyncSession, updates: dict, clear: list[str], by: str) -> None:
    """Store `updates` and remove the keys in `clear`, in the caller's transaction (the caller commits). Values must already be valid."""
    for key, value in updates.items():
        if key not in KEYS:
            raise KeyError(key)
        stored = {"enc": encrypt(value)} if key in SECRET_KEYS else value
        row = await db.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
        if row:
            row.value, row.updated_by, row.updated_at = stored, by[:255], datetime.utcnow()
        else:
            db.add(PlatformSetting(key=key, value=stored, updated_by=by[:255]))
    for key in clear:
        row = await db.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
        if row:
            await db.delete(row)
    await db.flush()
    invalidate()


async def stored_info(db: AsyncSession) -> dict[str, dict]:
    """{key: {'updated_at', 'updated_by'}} for what is stored, for the admin page."""
    return {r.key: {"updated_at": r.updated_at, "updated_by": r.updated_by} for r in (await db.scalars(select(PlatformSetting))) if r.key in KEYS}
