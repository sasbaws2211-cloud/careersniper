"""Which database revision are we at, and which does this code expect? (Alembic migrations in /migrations; read over the sync engine.)"""
from pathlib import Path

from .db import sync_engine

ROOT = Path(__file__).resolve().parent.parent


def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


def head_revision() -> str | None:
    """The newest migration shipped with this code, or None if alembic/migrations are unavailable."""
    try:
        return _script().get_current_head()
    except Exception:
        return None


def current_revision() -> str | None:
    """The revision the connected database is at (None = never migrated)."""
    from alembic.runtime.migration import MigrationContext
    with sync_engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def is_current() -> bool:
    head = head_revision()
    return head is None or current_revision() == head
