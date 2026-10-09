"""Relational persistence for dependency edges (no graph database required)."""

from __future__ import annotations

from collections.abc import Sequence

from orqentis.domain.topology import Dependency
from orqentis.storage.sqlite import SqliteDatabase

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS dependencies (
        id TEXT PRIMARY KEY,
        source_item_id TEXT NOT NULL,
        target_item_id TEXT NOT NULL,
        dependency_type TEXT NOT NULL,
        body TEXT NOT NULL,
        UNIQUE(source_item_id, target_item_id, dependency_type)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_dep_source ON dependencies(source_item_id)",
    "CREATE INDEX IF NOT EXISTS ix_dep_target ON dependencies(target_item_id)",
)


class SqliteDependencyStore:
    """Implements :class:`orqentis.topology.graph.DependencyStore`."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db
        db.migrate(_SCHEMA)

    async def list_edges(self) -> list[Dependency]:
        rows = await self._db.fetchall("SELECT body FROM dependencies ORDER BY id")
        return [Dependency.model_validate_json(r["body"]) for r in rows]

    async def replace_all(self, edges: Sequence[Dependency]) -> None:
        await self._db.execute("DELETE FROM dependencies")
        await self._db.executemany(
            "INSERT INTO dependencies (id, source_item_id, target_item_id, dependency_type, body) "
            "VALUES (?, ?, ?, ?, ?)",
            [(e.id, e.source_item_id, e.target_item_id, e.dependency_type.value, e.model_dump_json()) for e in edges],
        )


class InMemoryDependencyStore:
    def __init__(self, edges: Sequence[Dependency] = ()) -> None:
        self._edges = list(edges)

    async def list_edges(self) -> list[Dependency]:
        return list(self._edges)

    async def replace_all(self, edges: Sequence[Dependency]) -> None:
        self._edges = list(edges)
