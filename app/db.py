"""Database access. The app runs on an async engine (asyncpg); a small sync engine (psycopg2) exists only for
Alembic, the one-off SQLite copy, the CLI's blocking helpers and test seeding.

Tables are SQLModel classes (app/models.py); `Base` is kept as an alias so `Base.metadata` still works everywhere.
"""
import threading

from sqlalchemy import create_engine, make_url, pool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from . import config

Base = SQLModel


def async_url(url: str) -> str:
    u = make_url(url.replace("postgres://", "postgresql://", 1) if url.startswith("postgres://") else url)
    if u.get_backend_name() != "postgresql":
        raise RuntimeError("DATABASE_URL must be a PostgreSQL URL (postgresql+asyncpg://user:pw@host/db)")
    return u.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


def sync_url(url: str) -> str:
    return make_url(async_url(url)).set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)


# DB_POOL=null opens a connection per use: the test client starts a fresh event loop per client, and asyncpg
# connections cannot be reused across loops.
_pool = {"poolclass": pool.NullPool} if config.DB_POOL == "null" else {"pool_pre_ping": True, "pool_size": config.DB_POOL_SIZE,
                                                                       "max_overflow": config.DB_MAX_OVERFLOW}
engine = create_async_engine(async_url(config.DATABASE_URL), **_pool)
_web_factory = async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
_local = threading.local()


class _SessionProxy:
    """`SessionLocal()` opens a session on the web engine, or on the thread's private engine if `use_private_engine()` was called there
    (the scheduler thread runs its own event loop, and asyncpg connections cannot move between loops)."""

    def __call__(self, *a, **k):
        return (getattr(_local, "factory", None) or _web_factory)(*a, **k)


SessionLocal = _SessionProxy()

sync_engine = create_engine(sync_url(config.DATABASE_URL), poolclass=pool.NullPool)
SyncSessionLocal = sessionmaker(sync_engine, autoflush=False, expire_on_commit=False)


async def get_db():
    async with SessionLocal() as db:
        yield db


def new_engine():
    """A private async engine (no pooling) for code that runs on its own event loop."""
    return create_async_engine(async_url(config.DATABASE_URL), poolclass=pool.NullPool)


def use_private_engine():
    """Call once at the start of a thread that runs its own event loop. Returns the engine so the caller can dispose it."""
    eng = new_engine()
    _local.factory = async_sessionmaker(eng, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    _local.engine = eng
    return eng


def current_engine():
    return getattr(_local, "engine", None) or engine
