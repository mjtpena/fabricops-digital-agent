"""Start and drive application instances as real OS processes (see ``tests/support/instance.py``)."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from typing import Any

from orqentis.config.settings import Settings
from tests.conftest import REPO
from tests.support.postgres_app import AppStorage
from tests.support.services import AzuriteServer


def instance_args(storage: AppStorage, settings: Settings, **extra: object) -> dict[str, Any]:
    azurite: AzuriteServer = storage.azurite
    return {"settings": json.loads(settings.model_dump_json()), "azurite": asdict(azurite), **extra}


class Instance:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.stderr: list[str] = []
        self._drain = asyncio.create_task(self._drain_stderr())

    @classmethod
    async def start(cls, command: str, args: dict[str, Any]) -> Instance:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.instance",
            command,
            json.dumps(args),
            cwd=str(REPO),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return cls(process)

    async def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self.stderr.append(line.decode(errors="replace"))

    async def wait_for(self, prefix: str, *, within: float = 120.0) -> str:
        """The next output line that starts with ``prefix`` (earlier lines are skipped)."""
        assert self.process.stdout is not None

        async def read() -> str:
            assert self.process.stdout is not None
            while True:
                line = (await self.process.stdout.readline()).decode().strip()
                if not line:
                    if self.process.returncode is not None or self.process.stdout.at_eof():
                        raise AssertionError(f"instance ended before {prefix!r}: {''.join(self.stderr)[-2000:]}")
                    continue
                if line.startswith(prefix):
                    return line

        return await asyncio.wait_for(read(), within)

    async def go(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(b"GO\n")
        await self.process.stdin.drain()

    async def result(self, *, within: float = 180.0) -> dict[str, Any]:
        line = await self.wait_for("RESULT", within=within)
        await asyncio.wait_for(self.process.wait(), 60)
        loaded: dict[str, Any] = json.loads(line.removeprefix("RESULT "))
        return loaded

    async def kill(self) -> None:
        """The hardest stop there is: no shutdown hooks, no lease release, connections just drop."""
        if self.process.returncode is None:
            self.process.kill()
        await self.process.wait()
        self._drain.cancel()
        await asyncio.gather(self._drain, return_exceptions=True)
