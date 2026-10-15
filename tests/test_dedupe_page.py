import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.dedupe import page
from app.models import Job


class Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def test_page_gets_count_and_ids_in_one_query_when_page_has_rows():
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Rows([(11, 42)])),
        scalars=AsyncMock(return_value=[SimpleNamespace(id=11)]),
        scalar=AsyncMock(),
    )

    total, jobs = asyncio.run(page(db, select(Job), offset=0, limit=20))

    assert total == 42
    assert [job.id for job in jobs] == [11]
    assert db.execute.await_count == 1
    assert db.scalar.await_count == 0
    assert "count(*) over" in str(db.execute.await_args.args[0]).lower()


def test_page_falls_back_to_exact_count_for_empty_offset_page():
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Rows([])),
        scalars=AsyncMock(),
        scalar=AsyncMock(return_value=42),
    )

    total, jobs = asyncio.run(page(db, select(Job), offset=100, limit=20))

    assert total == 42
    assert jobs == []
    assert db.execute.await_count == 1
    assert db.scalar.await_count == 1
    assert db.scalars.await_count == 0
