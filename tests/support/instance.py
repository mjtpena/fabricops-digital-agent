"""A separate application instance (an OS process) for the multi-process tests.

    python -m tests.support.instance <command> '<json arguments>'

The instance builds the production container on the PostgreSQL storage backend (see ``postgres_app``), prints ``READY``
when it is up, and then waits for the test to print ``GO`` on its standard input, so that several instances start their
workload at the same moment. Results are printed as one JSON line starting with ``RESULT``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import asyncpg

from orqentis.actions.gateway import ActionBlockedError, ActionOutcome
from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import Settings
from orqentis.domain.actions import Action
from orqentis.domain.approvals import ApprovalRequest
from orqentis.runtime.investigations import InvestigationTrigger
from orqentis.storage.blob import BlobStorage
from tests.conftest import ADMIN, OPERATOR
from tests.support.postgres_app import azurite_blob_storage
from tests.support.reasoners import HangingReasoner
from tests.support.services import AzuriteServer


class DatabaseSpy:
    """The write capability: every call it receives is a row in a table all instances can see."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
        s = self._settings
        conn = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port, user=s.postgres_user,
                                     database=s.postgres_database)  # fmt: skip
        try:
            await conn.execute(
                f'INSERT INTO "{s.postgres_schema}".test_writes (pid, approval_id) VALUES ($1, $2)',  # noqa: S608
                os.getpid(),
                approval.approval_id,
            )
        finally:
            await conn.close()
        await asyncio.sleep(0.1)  # a slow write, so that racing executes overlap
        return {"executed": True}


def say(line: str) -> None:
    print(line, flush=True)


async def wait_for_go() -> None:
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)


async def run(command: str, args: dict[str, Any]) -> None:
    azurite = AzuriteServer(**args["azurite"])
    BlobStorage.open = staticmethod(azurite_blob_storage(azurite))  # type: ignore[method-assign]
    settings = Settings(_env_file=None, **args["settings"])  # type: ignore[arg-type]

    if command == "audit-load":
        container = await build_container(settings)
        try:
            say("READY")
            await wait_for_go()
            records = await asyncio.gather(
                *(
                    container.audit.record(actor=f"pid-{os.getpid()}", action="load", outcome="OK", details={"n": n})
                    for n in range(args["count"])
                )
            )
            say("RESULT " + json.dumps({"sequences": sorted(r.sequence for r in records), "pid": os.getpid()}))
        finally:
            await container.aclose()

    elif command == "execute-load":
        container = await build_container(settings, write_client=DatabaseSpy(settings))
        try:
            say("READY")
            await wait_for_go()
            results = await asyncio.gather(
                *(container.gateway.execute(args["approval_id"], principal=ADMIN) for _ in range(args["count"])),
                return_exceptions=True,
            )
            done = sum(isinstance(r, ActionOutcome) for r in results)
            codes = sorted(r.code for r in results if isinstance(r, ActionBlockedError))
            other = [repr(r) for r in results if isinstance(r, BaseException) and not isinstance(r, ActionBlockedError)]
            say("RESULT " + json.dumps({"executed": done, "blocked": codes, "other": other, "pid": os.getpid()}))
        finally:
            await container.aclose()

    elif command == "hang-investigation":
        reasoner = HangingReasoner()
        container: Container = await build_container(settings, reasoner=reasoner)
        await container.investigation_jobs.start()
        submission = await container.investigation_jobs.submit(
            args["incident_id"], requested_by=OPERATOR, trigger=InvestigationTrigger.API
        )
        await asyncio.wait_for(reasoner.started.wait(), 60)
        say("RUNNING " + json.dumps({"job_id": submission.job.job_id, "pid": os.getpid()}))
        await asyncio.Event().wait()  # until the test kills this process

    else:
        raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], json.loads(sys.argv[2])))
    sys.exit(0)
