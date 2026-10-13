"""Login throttling, CSRF tokens and password rules."""
from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict, deque

import asyncio

import bcrypt

from . import config

MAX_BYTES = 72                    # bcrypt reads no more than this
_DUMMY = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()
_fails: dict[str, deque] = defaultdict(deque)


def _recent(key: str, now: float) -> deque:
    q = _fails[key]
    while q and now - q[0] > config.LOGIN_WINDOW:
        q.popleft()
    return q


def locked(email: str, ip: str, now: float | None = None) -> bool:
    now = now or time.time()
    return len(_recent(f"a:{email}", now)) >= config.LOGIN_FAILS_PER_ACCOUNT or len(_recent(f"i:{ip}", now)) >= config.LOGIN_FAILS_PER_IP


def record_failure(email: str, ip: str, now: float | None = None) -> None:
    now = now or time.time()
    _recent(f"a:{email}", now).append(now)
    _recent(f"i:{ip}", now).append(now)


def clear_failures(email: str) -> None:
    _fails.pop(f"a:{email}", None)


def reset() -> None:
    _fails.clear()
    _stash.clear()


def _check_password(password: str, password_hash: str | None) -> bool:
    try:
        ok = bcrypt.checkpw((password or "").encode()[:MAX_BYTES], (password_hash or _DUMMY).encode())
    except ValueError:
        return False
    return ok and password_hash is not None


async def check_password(password: str, password_hash: str | None) -> bool:
    """Always does a bcrypt comparison, even for an account that does not exist, so timing does not say which emails are admins.
    bcrypt is slow on purpose, so it runs in a worker thread instead of stalling the event loop."""
    return await asyncio.to_thread(_check_password, password, password_hash)


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(lambda: bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())


def password_problem(password: str) -> str:
    if len(password) < config.MIN_PASSWORD:
        return f"Use at least {config.MIN_PASSWORD} characters."
    if len(password.encode()) > MAX_BYTES:
        return f"That password is too long ({MAX_BYTES} bytes at most)."
    if len(set(password)) < 5:
        return "Use a password with more variety than that."
    return ""


_stash: dict[str, tuple[float, int, str]] = {}
STASH_SECONDS, STASH_MAX = 600, 50


def stash(secret: str, admin_id: int, now: float | None = None) -> str:
    """Keep a just-typed secret in this process's memory (never in the page, a cookie or the database) and hand back a short-lived claim ticket
    bound to the admin who typed it."""
    now = now or time.time()
    for k in [k for k, (t, _, _) in _stash.items() if now - t > STASH_SECONDS]:
        _stash.pop(k, None)
    while len(_stash) >= STASH_MAX:
        _stash.pop(next(iter(_stash)))
    ticket = secrets.token_urlsafe(24)
    _stash[ticket] = (now, admin_id, secret)
    return ticket


def unstash(ticket: str, admin_id: int, now: float | None = None) -> str | None:
    now = now or time.time()
    entry = _stash.get(ticket or "")
    if not entry or now - entry[0] > STASH_SECONDS or entry[1] != admin_id:
        return None
    return entry[2]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())
