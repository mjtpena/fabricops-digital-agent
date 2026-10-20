"""PostgreSQL dependency store (relational edges; the replacement is atomic)."""

from __future__ import annotations

from collections.abc import Sequence

from orqentis.domain.topology import Dependency
from orqentis.storage.postgres.database import PostgresDatabase

# Arbitrary, fixed 64-bit key serialising replace_all ("ORQD" in ASCII). Two replicas that start together both
# rebuild the table; without the lock the second DELETE cannot see the first transaction's rows and its INSERT collides.
_REPLACE_LOCK_KEY = 0x4F525144


class PostgresDependencyStore:
    """Implements :class:`orqentis.topology.graph.DependencyStore`."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def list_edges(self) -> list[Dependency]:
        rows = await self._db.fetch("SELECT body FROM dependencies ORDER BY id")
        return [Dependency.model_validate_json(r["body"]) for r in rows]

    async def replace_all(self, edges: Sequence[Dependency]) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _REPLACE_LOCK_KEY)
            await conn.execute("DELETE FROM dependencies")
            await conn.executemany(
                "INSERT INTO dependencies (id, source_item_id, target_item_id, dependency_type, body) "
                "VALUES ($1, $2, $3, $4, $5)",
                [
                    (e.id, e.source_item_id, e.target_item_id, e.dependency_type.value, e.model_dump_json())
                    for e in edges
                ],
            )
