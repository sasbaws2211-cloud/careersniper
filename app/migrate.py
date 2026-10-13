"""Copy a CareerSniper SQLite database into Postgres (one-time move from dev to production).

    python -m app.cli migrate sqlite:///./careersniper.db postgresql://user:pw@host/dbname

The target must already have its schema (`alembic upgrade head`). Refuses to write into a database that already has data,
copies every table in dependency order, resets the id sequences, then builds the full-text search objects.
"""
from sqlalchemy import create_engine, inspect, literal, select, text

from .db import Base, sync_url
from .scrapers import clip
from .search import ensure_fts_sync

BATCH = 2000


def fill_defaults(t, row: dict) -> dict:
    """An older SQLite file can hold NULL in columns added later (nullable there, NOT NULL here):
    use the column's scalar default instead of failing the whole copy."""
    for c in t.columns:
        if row.get(c.name) is None and not c.nullable and c.default is not None and c.default.is_scalar:
            row[c.name] = c.default.arg
    return row


def migrate(source_url: str, target_url: str, log=print) -> dict:
    if not target_url.startswith(("postgres", "postgresql")):
        raise SystemExit("target must be a postgresql:// URL")
    src, dst = create_engine(source_url), create_engine(sync_url(target_url))
    missing = [t.name for t in Base.metadata.sorted_tables if not inspect(dst).has_table(t.name)]
    if missing:
        raise SystemExit(f"the target has no schema yet (missing {', '.join(missing[:3])}...): run `alembic upgrade head` against it first")
    with dst.connect() as c:
        for t in Base.metadata.sorted_tables:
            if c.execute(select(literal(1)).select_from(t).limit(1)).first():       # (not every table has an "id": company_logos does not)
                raise SystemExit(f"target table {t.name} already has rows; refusing to overwrite")
    insp_src = inspect(src)
    counts = {}
    for t in Base.metadata.sorted_tables:
        if not insp_src.has_table(t.name):
            continue
        cols = [c for c in t.columns if c.name in {x["name"] for x in insp_src.get_columns(t.name)}]
        n = 0
        with src.connect() as sc, dst.begin() as dc:
            result = sc.execute(select(*cols).execution_options(stream_results=True))
            while True:
                rows = result.fetchmany(BATCH)
                if not rows:
                    break
                model = next((m.class_ for m in Base._sa_registry.mappers if m.local_table is t), None)
                dc.execute(t.insert(), [fill_defaults(t, clip(model, dict(r._mapping)) if model else dict(r._mapping)) for r in rows])
                n += len(rows)
        counts[t.name] = n
        log(f"{t.name}: {n} rows")
    with dst.begin() as dc:   # explicit ids were inserted, so move each serial sequence past the max id
        for t in Base.metadata.sorted_tables:
            if "id" in t.c:
                dc.execute(text(f"SELECT setval(pg_get_serial_sequence('{t.name}', 'id'), "
                                f"COALESCE((SELECT MAX(id) FROM {t.name}), 1), (SELECT MAX(id) FROM {t.name}) IS NOT NULL)"))
    ensure_fts_sync(dst)
    log("full-text search ready")
    return counts
