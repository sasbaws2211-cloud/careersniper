"""Job text search.

Postgres: a stored, weighted tsvector (title+company A, department B, description C) with a GIN index,
queried with websearch_to_tsquery (supports "exact phrases", OR, -excluded) and ranked by ts_rank_cd.
If full-text finds nothing (e.g. a partial word like "engin"), fall back to substring matching, backed by a
trigram index when pg_trgm is available.
"""
import logging

from sqlalchemy import func, literal_column, or_, text

from .db import engine
from .models import Company, Job

log = logging.getLogger("careersniper.search")
VEC = literal_column("jobs.search_vector")


def is_pg() -> bool:
    return True      # PostgreSQL is the only supported database


def fts_clause(qs: str):
    """(WHERE clause, rank expression) for a Postgres full-text query."""
    tsq = func.websearch_to_tsquery(literal_column("'english'::regconfig"), qs)
    # Weights {D,C,B,A} = description 0.05, department 0.2, title/company 1.0, with normalisation 32
    # (rank/(rank+1)) so a long description repeating a word many times cannot outrank a title match.
    weights = literal_column("'{0.01,0.05,0.2,1.0}'::float4[]")
    return VEC.op("@@")(tsq), func.ts_rank_cd(weights, VEC, tsq, 32)


def like_conditions(qs: str) -> list:
    """Every word must appear in the title, company, department or description."""
    return [or_(func.lower(Job.title).contains(t), func.lower(Company.name).contains(t),
                func.lower(Job.department).contains(t), func.lower(Job.description).contains(t))
            for t in qs.lower().split()]


def setup_fts(conn) -> None:
    """Idempotent Postgres setup (sync Connection): denormalised company name, generated tsvector column, GIN + trigram indexes."""
    conn.execute(text("UPDATE jobs SET company_name = c.name FROM companies c "
                      "WHERE jobs.company_id = c.id AND jobs.company_name IS DISTINCT FROM c.name"))
    conn.execute(text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS search_vector tsvector GENERATED ALWAYS AS ("
        "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
        "setweight(to_tsvector('english'::regconfig, coalesce(company_name, '')), 'A') || "
        "setweight(to_tsvector('english'::regconfig, coalesce(department, '')), 'B') || "
        "setweight(to_tsvector('english'::regconfig, coalesce(description, '')), 'C')) STORED"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_search_vector ON jobs USING GIN (search_vector)"))


def setup_trgm(conn) -> None:
    """Trigram indexes speed up the substring fallback; they need the pg_trgm extension (optional)."""
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_title_trgm ON jobs USING GIN (lower(title) gin_trgm_ops)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_company_trgm ON jobs USING GIN (lower(company_name) gin_trgm_ops)"))


async def ensure_fts(eng=None) -> None:
    """Run the Postgres search setup on the async engine (or the one given)."""
    eng = eng or engine
    async with eng.begin() as c:
        await c.run_sync(setup_fts)
    try:
        async with eng.begin() as c:
            await c.run_sync(setup_trgm)
    except Exception:
        log.warning("pg_trgm unavailable; substring fallback will use sequential scans")


def ensure_fts_sync(eng) -> None:
    """Same setup for a sync engine (the SQLite copy tool)."""
    with eng.begin() as c:
        setup_fts(c)
    try:
        with eng.begin() as c:
            setup_trgm(c)
    except Exception:
        log.warning("pg_trgm unavailable; substring fallback will use sequential scans")
