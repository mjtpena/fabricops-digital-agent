"""Process lifecycle of the API: background container build, scheduler start and ordered shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from orqentis.config.settings import FabricMode, Settings
from orqentis.observability.logging import get_logger
from orqentis.runtime.supervisor import BuildStatus, BuildSupervisor

if TYPE_CHECKING:
    from orqentis.bootstrap import Container

_log = get_logger(__name__)


class Runtime:
    """Owns the container and the autonomous parts (scheduler, investigation jobs) for one process.

    ``start`` returns quickly: the container is built in the background and the process answers liveness probes while
    that happens. A container passed in by the caller (tests, embedding) is used as is and is never closed here.
    """

    def __init__(
        self,
        settings: Settings,
        factory: Callable[[Settings], Awaitable[Container]],
        *,
        container: Container | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._injected = container
        self._container: Container | None = None
        self._task: asyncio.Task[None] | None = None
        self._supervisor: BuildSupervisor[Container] = BuildSupervisor(
            self._build,
            initial_backoff=settings.build_retry_initial_seconds,
            max_backoff=settings.build_retry_max_seconds,
            sleep=sleep,
        )

    # --------------------------------------------------------------------------------------------- state
    @property
    def container(self) -> Container | None:
        # A container handed in by the caller is usable even before (or without) the lifespan having started.
        return self._container if self._container is not None else self._injected

    @property
    def ready(self) -> bool:
        return self.container is not None

    @property
    def status(self) -> BuildStatus:
        return BuildStatus(True, 0, None, None, None) if self._injected is not None else self._supervisor.status

    # ------------------------------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._injected is not None:
            await self._become_ready(self._injected)
            return
        self._task = asyncio.create_task(self._supervise(), name="container-build")
        # A fast build (the mock estate) is ready before the first request; a slow one must not hold the port.
        await self._supervisor.wait_first_attempt(self._settings.startup_grace_seconds)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        container, self._container = self._container, None
        if container is None:
            return
        await container.replica_sync.stop()
        await container.scheduler.stop()
        await container.investigation_jobs.shutdown()
        await container.notifications.stop()  # last: the jobs above may still publish while they wind down
        if self._injected is None:
            await container.aclose()

    # -------------------------------------------------------------------------------------------- internals
    async def _build(self) -> Container:
        container = await self._factory(self._settings)
        try:
            if self._settings.fabric_mode is FabricMode.MOCK:
                # The synthetic estate is seeded at start so a demo server has an incident to show immediately.
                _, created = await container.collect_and_detect()
                _log.info("mock telemetry collected", extra={"incidents_created": [i.incident_id for i in created]})
        except BaseException:
            await container.aclose()
            raise
        return container

    async def _supervise(self) -> None:
        await self._become_ready(await self._supervisor.run())

    async def _become_ready(self, container: Container) -> None:
        self._container = container
        # Every replica works on queued investigations (a shared job store), whether or not it runs the scheduler.
        await container.investigation_jobs.start()
        await container.notifications.start()
        if self._settings.scheduler_enabled:
            await container.scheduler.start()
            if self._settings.scheduler_catalog_refresh_enabled:
                await container.replica_sync.start()
        else:
            _log.info("scheduler disabled (ORQENTIS_SCHEDULER_ENABLED=false)")
