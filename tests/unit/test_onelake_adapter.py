"""The OneLake adapter: two read-only GETs against the ADLS Gen2 endpoint, bounded in what they read."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from orqentis.domain.delta import DeltaTableRef
from orqentis.fabric.errors import FabricAuthError, FabricError, FabricNotFoundError
from orqentis.fabric.rest.http import FabricHttp
from orqentis.fabric.rest.onelake import STORAGE_API_VERSION, RestDeltaLogService

BASE = "https://onelake.example.invalid"
SCOPE = "https://storage.azure.com/.default"
WORKSPACE = "00000000-0000-4000-8000-0000000000c1"
LAKEHOUSE = "00000000-0000-4000-8000-0000000000c2"
TABLE = DeltaTableRef(id="t1", item_id=LAKEHOUSE, workspace_id=WORKSPACE, table="customers")


class Tokens:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def get_token(self, scope: str) -> str:
        self.scopes.append(scope)
        return "token-not-real"


async def _no_sleep(_: float) -> None:
    return None


def service(
    handler: Callable[[httpx.Request], httpx.Response], *, cap: int = 4096
) -> tuple[RestDeltaLogService, Tokens, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    tokens = Tokens()
    http = FabricHttp(tokens, transport=httpx.MockTransport(record), max_retries=0, sleep=_no_sleep)
    return RestDeltaLogService(http, base_url=BASE, scope=SCOPE, max_commit_bytes=cap), tokens, seen


def listing(*names: str, directories: tuple[str, ...] = ()) -> dict[str, object]:
    prefix = f"{LAKEHOUSE}/Tables/customers/_delta_log/"
    paths: list[dict[str, str]] = [
        {"name": prefix + n, "contentLength": "100", "lastModified": "Tue, 20 Oct 2026 01:00:00 GMT"} for n in names
    ]
    paths += [{"name": prefix + d, "isDirectory": "true"} for d in directories]
    return {"paths": paths}


async def test_listing_keeps_only_json_commits_in_version_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=listing(
                "00000000000000000002.json",
                "00000000000000000000.json",
                "00000000000000000010.checkpoint.parquet",
                "_last_checkpoint",
                "00000000000000000001.crc",
                "00000000000000000001.json",
                directories=("_staged_commits",),
            ),
        )

    svc, tokens, seen = service(handler)
    commits = await svc.list_commits(TABLE)
    assert [c.version for c in commits] == [0, 1, 2]
    assert commits[0].modified_at is not None and commits[0].modified_at.year == 2026
    request = seen[0]
    assert request.method == "GET" and request.url.path == f"/{WORKSPACE}"
    params = dict(request.url.params)
    assert params["resource"] == "filesystem" and params["recursive"] == "false"
    assert params["directory"] == f"{LAKEHOUSE}/Tables/customers/_delta_log"
    assert request.headers["x-ms-version"] == STORAGE_API_VERSION
    assert tokens.scopes == [SCOPE]


async def test_listing_follows_the_continuation_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "continuation" not in request.url.params:
            return httpx.Response(
                200, json=listing("00000000000000000000.json"), headers={"x-ms-continuation": "next-1"}
            )
        assert request.url.params["continuation"] == "next-1"
        return httpx.Response(200, json=listing("00000000000000000001.json"))

    svc, _, seen = service(handler)
    assert [c.version for c in await svc.list_commits(TABLE)] == [0, 1]
    assert len(seen) == 2


async def test_a_schema_qualified_table_reads_below_its_schema() -> None:
    table = DeltaTableRef(id="t", item_id=LAKEHOUSE, workspace_id=WORKSPACE, table="orders", schema_name="sales")
    svc, _, seen = service(lambda r: httpx.Response(200, json={"paths": []}))
    assert await svc.list_commits(table) == []
    assert dict(seen[0].url.params)["directory"] == f"{LAKEHOUSE}/Tables/sales/orders/_delta_log"

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"commitInfo":{"operation":"WRITE"}}\n')

    svc2, _, seen2 = service(ok)
    commit = await svc2.read_commit(table, 7)
    assert seen2[0].url.path == f"/{WORKSPACE}/{LAKEHOUSE}/Tables/sales/orders/_delta_log/00000000000000000007.json"
    assert commit.version == 7 and len(commit.lines) == 1


async def test_a_commit_keeps_only_the_schema_and_provenance_actions() -> None:
    body = "\n".join(
        [
            '{"commitInfo":{"operation":"WRITE"}}',
            '{"protocol":{"minReaderVersion":1}}',
            '{"metaData":{"schemaString":"{}"}}',
            '{"add":{"path":"part-1.parquet"}}',
            '{ "add" : {"path":"part-2.parquet"}}',
            '{"remove":{"path":"part-0.parquet"}}',
            "",
        ]
    ).encode()
    svc, _, seen = service(lambda r: httpx.Response(200, content=body))
    commit = await svc.read_commit(TABLE, 3)
    assert [json.loads(line).keys().__iter__().__next__() for line in commit.lines] == [
        "commitInfo",
        "protocol",
        "metaData",
    ]
    assert not commit.truncated and commit.size_bytes == len(body)
    assert seen[0].method == "GET" and seen[0].headers["range"] == "bytes=0-4095"


async def test_a_large_commit_is_read_only_up_to_the_cap_and_flagged() -> None:
    head = b'{"commitInfo":{"operation":"WRITE"}}\n{"metaData":{"schemaString":"{}"}}\n'
    padding = b'{"add":{"path":"' + b"x" * 200 + b'"}}\n'
    partial = head + padding * 10
    total = 5_000_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(206, content=partial, headers={"Content-Range": f"bytes 0-{len(partial) - 1}/{total}"})

    svc, _, _ = service(handler, cap=len(partial))
    commit = await svc.read_commit(TABLE, 1)
    assert commit.truncated and commit.size_bytes == total
    assert len(commit.lines) == 2  # the cut-off last line is dropped; the add actions are never kept


async def test_a_server_that_ignores_range_cannot_make_us_keep_the_whole_file() -> None:
    body = b'{"commitInfo":{"operation":"WRITE"}}\n' + b'{"add":{"path":"p"}}\n' * 1000
    svc, _, _ = service(lambda r: httpx.Response(200, content=body), cap=1024)
    commit = await svc.read_commit(TABLE, 1)
    assert commit.truncated and len(commit.lines) == 1


@pytest.mark.parametrize(
    ("status", "error"), [(403, FabricAuthError), (401, FabricAuthError), (404, FabricNotFoundError)]
)
async def test_http_failures_become_typed_errors(status: int, error: type[FabricError]) -> None:
    body = {"error": {"code": "AuthorizationPermissionMismatch", "message": "not allowed"}}
    svc, _, _ = service(lambda r: httpx.Response(status, json=body))
    with pytest.raises(error):
        await svc.list_commits(TABLE)
    with pytest.raises(error):
        await svc.read_commit(TABLE, 0)


async def test_an_unreadable_listing_is_an_error_not_an_empty_log() -> None:
    svc, _, _ = service(lambda r: httpx.Response(200, content=b"<html>not json</html>"))
    with pytest.raises(FabricError) as excinfo:
        await svc.list_commits(TABLE)
    assert excinfo.value.error_code == "InvalidResponse"


async def test_only_get_requests_are_ever_sent() -> None:
    svc, _, seen = service(lambda r: httpx.Response(200, json=listing("00000000000000000000.json")))
    await svc.list_commits(TABLE)
    await svc.read_commit(TABLE, 0)
    assert {r.method for r in seen} == {"GET"}


async def test_a_version_cannot_be_negative_and_the_cap_has_a_floor() -> None:
    svc, _, seen = service(lambda r: httpx.Response(200))
    with pytest.raises(FabricError):
        await svc.read_commit(TABLE, -1)
    assert seen == []
    with pytest.raises(ValueError, match="at least 1024"):
        RestDeltaLogService(FabricHttp(Tokens()), base_url=BASE, scope=SCOPE, max_commit_bytes=10)
