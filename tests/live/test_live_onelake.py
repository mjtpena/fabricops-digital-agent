"""OneLake `_delta_log` read access for the test identity (the grant behind the schema-drift collector, ADR-0009).

Findings these tests pin down (details in docs/security.md):

* Workspace Viewer alone gives NO OneLake data access: every path is 403.
* A OneLake data access role granting ``Read`` on ``Tables/<table>`` to the principal is enough to list and
  read that table's ``_delta_log``. It is read-only, scoped to that table, and needs no write right of any kind.
  (Roles can be defined only at the lakehouse root or table level; there is no finer ``_delta_log`` scope.)
* The role gives nothing else: other tables, ``Files`` and every write stay 403.

The DEV sandbox lakehouse carries the role (``fabricopsDeltaLogReader`` on ``Tables/customers``); the TEST
lakehouse deliberately does not and is the control.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from azure.identity.aio import EnvironmentCredential

from orqentis.domain.estate import Workspace
from orqentis.fabric.rest.client import RestFabricClient
from tests.live.conftest import LAKEHOUSE, Sandbox

ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
STORAGE_SCOPE = "https://storage.azure.com/.default"
DELTA_LOG = "Tables/customers/_delta_log"


@pytest.fixture
async def onelake() -> AsyncIterator[httpx.AsyncClient]:
    credential = EnvironmentCredential()
    token = (await credential.get_token(STORAGE_SCOPE)).token
    async with httpx.AsyncClient(
        base_url=ONELAKE, timeout=30, headers={"Authorization": f"Bearer {token}", "x-ms-version": "2023-11-03"}
    ) as client:
        yield client
    await credential.close()


async def _lakehouse_id(client: RestFabricClient, workspace: Workspace) -> str:
    items = await client.items.list_items(workspace.id)
    return next(i.id for i in items if i.display_name == LAKEHOUSE and i.item_type.value == "Lakehouse")


async def _list(onelake: httpx.AsyncClient, workspace: Workspace, lakehouse_id: str, directory: str) -> httpx.Response:
    return await onelake.get(
        f"/{workspace.id}",
        params={"resource": "filesystem", "directory": f"{lakehouse_id}/{directory}", "recursive": "false"},
    )


async def test_delta_log_is_readable_with_a_table_level_read_only_role(
    onelake: httpx.AsyncClient, rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    dev = sandbox.workspace("[DEV]")
    lakehouse = await _lakehouse_id(rest_client, dev)

    listing = await _list(onelake, dev, lakehouse, DELTA_LOG)
    assert listing.status_code == 200
    names = sorted(p["name"].rsplit("/", 1)[-1] for p in listing.json()["paths"])
    assert "00000000000000000000.json" in names

    commit = await onelake.get(f"/{dev.id}/{lakehouse}/{DELTA_LOG}/00000000000000000000.json")
    assert commit.status_code == 200
    actions = [json.loads(line) for line in commit.text.splitlines() if line.strip()]
    metadata = next(a["metaData"] for a in actions if "metaData" in a)
    columns = [f["name"] for f in json.loads(metadata["schemaString"])["fields"]]
    assert "full_name" in columns  # schema history is what the drift collector diffs


async def test_the_role_grants_nothing_beyond_the_one_table(
    onelake: httpx.AsyncClient, rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    dev = sandbox.workspace("[DEV]")
    lakehouse = await _lakehouse_id(rest_client, dev)

    tables = await _list(onelake, dev, lakehouse, "Tables")
    assert [p["name"].rsplit("/", 1)[-1] for p in tables.json()["paths"]] == ["customers"]
    assert (await _list(onelake, dev, lakehouse, "Tables/customers_clean/_delta_log")).status_code == 403
    assert (await _list(onelake, dev, lakehouse, "Files")).status_code == 403


async def test_no_write_is_possible_even_with_the_read_role(
    onelake: httpx.AsyncClient, rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    dev = sandbox.workspace("[DEV]")
    lakehouse = await _lakehouse_id(rest_client, dev)
    for path in ("Files/denied.txt", "Tables/customers/denied.parquet"):
        created = await onelake.put(f"/{dev.id}/{lakehouse}/{path}", params={"resource": "file"})
        assert created.status_code == 403, path


async def test_workspace_viewer_alone_cannot_read_onelake(
    onelake: httpx.AsyncClient, rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    """TEST has the Viewer assignment and no data access role: the control for the grant above."""
    test = sandbox.workspace("[TEST]")
    lakehouse = await _lakehouse_id(rest_client, test)
    for directory in ("Tables", "Tables/customers/_delta_log", "Files"):
        assert (await _list(onelake, test, lakehouse, directory)).status_code == 403, directory
    commit = await onelake.get(f"/{test.id}/{lakehouse}/{DELTA_LOG}/00000000000000000000.json")
    assert commit.status_code == 403
