"""Alembic environment (async): runs on the app's own DATABASE_URL through asyncpg and diffs against the SQLModel metadata,
so `alembic revision --autogenerate` sees every table.

Not tracked here (created idempotently at app start, they depend on Postgres features): the jobs.search_vector generated
column and its GIN/trigram indexes (app/search.py ensure_fts).
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from app import config
from app import models  # noqa: F401  (registers every table on SQLModel.metadata)
from app.db import async_url

cfg = context.config
if cfg.config_file_name is not None:
    fileConfig(cfg.config_file_name)
target_metadata = SQLModel.metadata


def include_object(obj, name, type_, reflected, compare_to):
    # objects created outside the models by ensure_fts(): never drop or diff them
    if type_ == "index" and name in ("ix_jobs_search_vector", "ix_jobs_title_trgm", "ix_jobs_company_trgm"):
        return False
    if type_ == "column" and name == "search_vector":
        return False
    return True


def run_offline() -> None:
    context.configure(url=async_url(config.DATABASE_URL), target_metadata=target_metadata, literal_binds=True, compare_type=True,
                      include_object=include_object, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(async_url(config.DATABASE_URL), poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
