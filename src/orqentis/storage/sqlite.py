"""Minimal async wrapper over sqlite3 for the local (development) adapters."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

Params = Sequence[Any] | dict[str, Any]


class SqliteDatabase:
    def __init__(self, path: Path | str) -> None:
        target = str(path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if target != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()

    def migrate(self, statements: Iterable[str]) -> None:
        with self._lock:
            for statement in statements:
                self._conn.execute(statement)

    def _execute(self, sql: str, params: Params) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def _executemany(self, sql: str, rows: Sequence[Params]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _fetchall(self, sql: str, params: Params) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    async def execute(self, sql: str, params: Params = ()) -> int:
        return await asyncio.to_thread(self._execute, sql, params)

    async def executemany(self, sql: str, rows: Sequence[Params]) -> None:
        await asyncio.to_thread(self._executemany, sql, rows)

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._fetchall, sql, params)

    async def fetchone(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
