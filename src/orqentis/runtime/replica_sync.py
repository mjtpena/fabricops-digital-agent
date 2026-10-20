"""Keep every replica's in-memory catalog and contracts current, not only the leader's.

The catalog (workspaces, items, data products, SLAs), the dependency graph and the contracts in force live in each
process's memory, and investigations run on whichever replica claims the job. The ``catalog-refresh`` scheduler job runs
on the leader only, so without this a follower would keep the estate and the contracts it started with until the next
restart, and a new contracts version uploaded to the blob would reach one replica in two.

Each replica therefore refreshes **its own memory** on the same interval (``ORQENTIS_SCHEDULER_CATALOG_REFRESH_*``).
The leader skips it (its scheduled job does the same work and, in addition, the once-per-change shared effects: the
shared dependency table, the audit of the diff, the incident annotations). A failing run is logged and retried on the
next interval; it never stops the loop, and the replica keeps serving the contracts and catalog it has.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from orqentis.observability.logging import get_logger
from orqentis.observability.redaction import redact_text
from orqentis.topology.refresh import CatalogRefresher

_log = get_logger(__name__)


class ReplicaCatalogSync:
    def __init__(
        self,
        refresher: CatalogRefresher,
        is_leader: Callable[[], bool],
        interval: timedelta,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if interval <= timedelta(0):
            raise ValueError("interval must be positive")
        self._refresher = refresher
        self._is_leader = is_leader
        self._interval = interval
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._failing = False

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.is_running:
            self._task = asyncio.create_task(self._loop(), name="replica-catalog-sync")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run_once(self) -> bool:
        """One local refresh (unless this replica leads). Returns False when it was skipped or failed."""
        if self._is_leader():
            return False  # the scheduled job on the leader refreshes this replica's memory too
        try:
            await self._refresher.refresh(shared_effects=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._failing:  # log the change of health, not every retry
                _log.warning(
                    "replica catalog refresh failed; the current catalog and contracts stay in force",
                    extra={"error": f"{type(exc).__name__}: {redact_text(str(exc))}"[:500]},
                )
            self._failing = True
            return False
        if self._failing:
            _log.info("replica catalog refresh recovered")
        self._failing = False
        return True

    async def _loop(self) -> None:
        # The catalog and contracts were loaded when the container was built, so the first run is one interval away.
        while True:
            await self._sleep(self._interval.total_seconds())
            await self.run_once()
