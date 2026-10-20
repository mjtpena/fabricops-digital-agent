"""RestFabricClient against an in-process ``httpx.MockTransport`` (no network, no Azure credentials)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError

from orqentis.config.clock import FixedClock
from orqentis.config.settings import AuthMode, Settings
from orqentis.contracts import EstateContracts
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.estate import GenericItem, Lakehouse, Notebook, Pipeline, Report, SemanticModel, Warehouse
from orqentis.domain.runs import RunStatus
from orqentis.domain.topology import Dependency, DependencyType
from orqentis.fabric.errors import (
    FabricAuthError,
    FabricCapabilityUnavailableError,
    FabricError,
    FabricNotFoundError,
    FabricRateLimitedError,
    FabricUnavailableError,
)
from orqentis.fabric.ports import FabricClient
from orqentis.fabric.rest.auth import (
    FABRIC_SCOPE,
    POWERBI_SCOPE,
    AzureIdentityTokenProvider,
    build_async_credential,
    build_sync_credential,
)
from orqentis.fabric.rest.client import RestFabricClient, infer_environment, stage_environment, target_item_id
from orqentis.fabric.rest.http import CLIENT_REQUEST_ID_HEADER, FabricHttp

FABRIC = "https://api.fabric.microsoft.com/v1"
POWERBI = "https://api.powerbi.com/v1.0/myorg"
FABRIC_PATH = "/v1"
POWERBI_PATH = "/v1.0/myorg"
TOKENS = {FABRIC_SCOPE: "fabric-SECRET-token-abc", POWERBI_SCOPE: "powerbi-SECRET-token-xyz"}
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

Responder = Callable[[httpx.Request], httpx.Response]


# ------------------------------------------------------------------------------------------ doubles
class FakeTokenProvider:
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.closed = False

    async def get_token(self, scope: str) -> str:
        self.scopes.append(scope)
        return TOKENS[scope]

    async def close(self) -> None:
        self.closed = True


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class Router:
    """MockTransport handler: (method, path) -> queued responders; the last responder repeats."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Responder]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, path: str, *responders: Responder) -> None:
        self.routes.setdefault((method, path), []).extend(responders)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        return responder(request)


def respond(status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> Responder:
    def responder(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return responder


def connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def fabric_error(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"errorCode": code, "message": message, "requestId": request_id}


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def tokens() -> FakeTokenProvider:
    return FakeTokenProvider()


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


def make_client(
    router: Router,
    tokens: FakeTokenProvider,
    sleeper: SleepRecorder,
    *,
    contracts: EstateContracts | None = None,
    max_retries: int = 4,
) -> RestFabricClient:
    return RestFabricClient(
        fabric_base_url=FABRIC,
        powerbi_base_url=POWERBI,
        token_provider=tokens,
        contracts=contracts or EstateContracts(),
        tenant_id="tenant-test",
        max_retries=max_retries,
        transport=httpx.MockTransport(router),
        sleep=sleeper,
        clock=FixedClock(NOW),
    )


@pytest.fixture
async def client(router: Router, tokens: FakeTokenProvider, sleeper: SleepRecorder) -> AsyncIterator[RestFabricClient]:
    fabric = make_client(router, tokens, sleeper)
    yield fabric
    await fabric.aclose()


@pytest.fixture
def orqentis_logs(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture ``orqentis.*`` records even if configure_logging() has set propagate=False elsewhere."""
    logger = logging.getLogger("orqentis")
    attach = not logger.propagate  # caplog's handler sits on the root logger
    if attach:
        logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="orqentis")
    try:
        yield caplog
    finally:
        if attach:
            logger.removeHandler(caplog.handler)


# ------------------------------------------------------------------------------------- port / safety
async def test_satisfies_fabric_client_port(client: RestFabricClient) -> None:
    port: FabricClient = client  # mypy-checked structural conformance
    for name in (
        "workspaces",
        "items",
        "jobs",
        "pipelines",
        "semantic_models",
        "capacities",
        "deployments",
        "dependencies",
    ):
        assert getattr(port, name) is not None


async def test_client_and_services_expose_no_mutation_methods(client: RestFabricClient) -> None:
    forbidden = ("create", "update", "delete", "run", "execute", "cancel", "refresh")
    also_forbidden = ("deploy_", "assign_", "trigger_", "start_", "stop_", "post_", "put_", "patch_")
    services = [
        client.workspaces,
        client.items,
        client.jobs,
        client.pipelines,
        client.semantic_models,
        client.capacities,
        client.deployments,
        client.dependencies,
    ]
    for obj in [client, *services]:
        public = [n.lower() for n in dir(obj) if not n.startswith("_")]
        offending = [n for n in public if n.startswith(forbidden) or n.startswith(also_forbidden)]
        assert not offending, f"{type(obj).__name__} exposes mutation-like methods: {offending}"


async def test_from_settings_builds_client() -> None:
    settings = Settings(tenant_id="tenant-from-settings", http_max_retries=2, http_timeout_seconds=5)
    tokens = FakeTokenProvider()
    fabric = RestFabricClient.from_settings(settings, EstateContracts(), tokens)
    assert isinstance(fabric, RestFabricClient)
    await fabric.aclose()
    assert tokens.closed


async def test_invalid_identifier_is_rejected_without_a_request(client: RestFabricClient, router: Router) -> None:
    with pytest.raises(FabricError) as excinfo:
        await client.workspaces.get_workspace("..")
    assert excinfo.value.error_code == "InvalidIdentifier"
    assert router.requests == []


# ----------------------------------------------------------------------------- auth & correlation
async def test_fabric_calls_use_fabric_scope_bearer_and_client_request_id(
    client: RestFabricClient, router: Router, tokens: FakeTokenProvider
) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces",
        respond(body={"value": [{"id": "ws-1", "displayName": "Sales [PROD]", "capacityId": "cap-1"}]}),
    )
    await client.workspaces.list_workspaces()
    (request,) = router.requests
    assert request.headers["Authorization"] == f"Bearer {TOKENS[FABRIC_SCOPE]}"
    assert tokens.scopes == [FABRIC_SCOPE]
    uuid.UUID(request.headers[CLIENT_REQUEST_ID_HEADER])  # a fresh UUID per logical request


async def test_powerbi_calls_use_powerbi_scope(
    client: RestFabricClient, router: Router, tokens: FakeTokenProvider
) -> None:
    router.add("GET", f"{POWERBI_PATH}/groups/ws-1/datasets/sm-1/refreshes", respond(body={"value": []}))
    await client.semantic_models.get_refresh_history("ws-1", "sm-1", top=5)
    (request,) = router.requests
    assert request.headers["Authorization"] == f"Bearer {TOKENS[POWERBI_SCOPE]}"
    assert request.url.params["$top"] == "5"
    assert tokens.scopes == [POWERBI_SCOPE]


async def test_each_logical_request_gets_its_own_correlation_id(client: RestFabricClient, router: Router) -> None:
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-1", respond(body={"id": "ws-1", "displayName": "A"}))
    await client.workspaces.get_workspace("ws-1")
    await client.workspaces.get_workspace("ws-1")
    ids = {r.headers[CLIENT_REQUEST_ID_HEADER] for r in router.requests}
    assert len(ids) == 2


# ---------------------------------------------------------------------------------------- pagination
async def test_pagination_follows_continuation_uri(client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces",
        respond(
            body={
                "value": [{"id": "ws-1", "displayName": "One"}],
                "continuationToken": "abc",
                "continuationUri": f"{FABRIC}/workspaces?continuationToken=abc",
            }
        ),
        respond(body={"value": [{"id": "ws-2", "displayName": "Two"}]}),
    )
    workspaces = await client.workspaces.list_workspaces()
    assert [w.id for w in workspaces] == ["ws-1", "ws-2"]
    assert len(router.requests) == 2
    assert router.requests[1].url.params["continuationToken"] == "abc"


async def test_pagination_via_continuation_token_keeps_original_params(
    client: RestFabricClient, router: Router
) -> None:
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-1", respond(body={"id": "ws-1", "displayName": "Sales DEV"}))
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces/ws-1/items",
        respond(
            body={"value": [{"id": "nb-1", "type": "Notebook", "displayName": "nb1"}], "continuationToken": "tok-1"}
        ),
        respond(body={"value": [{"id": "nb-2", "type": "Notebook", "displayName": "nb2"}]}),
    )
    items = await client.items.list_items("ws-1", item_type=ItemType.NOTEBOOK)
    assert [i.id for i in items] == ["nb-1", "nb-2"]
    first, second = router.calls("GET", f"{FABRIC_PATH}/workspaces/ws-1/items")
    assert first.url.params["type"] == "Notebook"
    assert "continuationToken" not in first.url.params
    assert second.url.params["type"] == "Notebook"
    assert second.url.params["continuationToken"] == "tok-1"


async def test_pagination_stops_on_repeated_continuation_token(client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/capacities",
        respond(body={"value": [{"id": "cap-1", "displayName": "C", "sku": "F2"}], "continuationToken": "same"}),
    )
    capacities = await client.capacities.list_capacities()
    assert len(router.requests) == 2  # second page repeats the token -> stop instead of looping forever
    assert [c.id for c in capacities] == ["cap-1", "cap-1"]


async def test_pagination_never_follows_continuation_uri_to_another_host(
    client: RestFabricClient, router: Router
) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/capacities",
        respond(
            body={
                "value": [{"id": "cap-1", "displayName": "C", "sku": "F2"}],
                "continuationUri": "https://attacker.example.com/steal",
            }
        ),
    )
    capacities = await client.capacities.list_capacities()
    assert len(capacities) == 1
    assert all(r.url.host == "api.fabric.microsoft.com" for r in router.requests)


# ------------------------------------------------------------------------------- retries & errors
async def test_429_with_retry_after_is_retried_then_succeeds(
    client: RestFabricClient, router: Router, sleeper: SleepRecorder
) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces/ws-1",
        respond(429, fabric_error("TooManyRequests", "Slow down", "req-429"), headers={"Retry-After": "7"}),
        respond(body={"id": "ws-1", "displayName": "Sales [PROD]"}),
    )
    workspace = await client.workspaces.get_workspace("ws-1")
    assert workspace.environment is Environment.PROD
    assert sleeper.delays == [7.0]
    first, second = router.requests
    assert first.headers[CLIENT_REQUEST_ID_HEADER] == second.headers[CLIENT_REQUEST_ID_HEADER]


async def test_retry_after_is_capped(client: RestFabricClient, router: Router, sleeper: SleepRecorder) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces/ws-1",
        respond(429, headers={"Retry-After": "3600"}),
        respond(body={"id": "ws-1", "displayName": "A"}),
    )
    await client.workspaces.get_workspace("ws-1")
    assert sleeper.delays == [60.0]


async def test_429_exhausted_raises_rate_limited(
    router: Router, tokens: FakeTokenProvider, sleeper: SleepRecorder
) -> None:
    fabric = make_client(router, tokens, sleeper, max_retries=2)
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces/ws-1",
        respond(
            429,
            fabric_error("TooManyRequests", "Throttled", "req-body"),
            headers={"Retry-After": "3", "RequestId": "req-header"},
        ),
    )
    with pytest.raises(FabricRateLimitedError) as excinfo:
        await fabric.workspaces.get_workspace("ws-1")
    await fabric.aclose()
    error = excinfo.value
    assert len(router.requests) == 3
    assert sleeper.delays == [3.0, 3.0]
    assert error.status_code == 429
    assert error.retry_after_seconds == 3.0
    assert error.retryable is True
    assert error.error_code == "TooManyRequests"
    assert error.request_id == "req-header"  # response header wins over the body
    assert error.correlation_id == router.requests[0].headers[CLIENT_REQUEST_ID_HEADER]
    assert len({r.headers[CLIENT_REQUEST_ID_HEADER] for r in router.requests}) == 1


async def test_503_is_retried_with_backoff(client: RestFabricClient, router: Router, sleeper: SleepRecorder) -> None:
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-1", respond(503), respond(body={"id": "ws-1", "displayName": "A"}))
    await client.workspaces.get_workspace("ws-1")
    assert len(router.requests) == 2
    (delay,) = sleeper.delays
    assert 1.0 <= delay <= 2.0  # base 1s + jitter


async def test_backoff_is_exponential_with_jitter(router: Router, tokens: FakeTokenProvider) -> None:
    sleeper = SleepRecorder()
    http = FabricHttp(tokens, max_retries=3, transport=httpx.MockTransport(router), sleep=sleeper, jitter=lambda: 0.5)
    router.add("GET", "/v1/x", respond(502), respond(500), respond(504), respond(body={"ok": True}))
    assert await http.get_json(f"{FABRIC}/x", scope=FABRIC_SCOPE) == {"ok": True}
    await http.aclose()
    assert sleeper.delays == [1.5, 2.5, 4.5]


async def test_5xx_exhausted_raises_unavailable(
    router: Router, tokens: FakeTokenProvider, sleeper: SleepRecorder
) -> None:
    fabric = make_client(router, tokens, sleeper, max_retries=1)
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-1", respond(503, headers={"x-ms-request-id": "req-503"}))
    with pytest.raises(FabricUnavailableError) as excinfo:
        await fabric.workspaces.get_workspace("ws-1")
    await fabric.aclose()
    assert excinfo.value.retryable is True
    assert excinfo.value.status_code == 503
    assert excinfo.value.request_id == "req-503"
    assert len(router.requests) == 2


async def test_transport_errors_are_retried_then_raise_unavailable(
    router: Router, tokens: FakeTokenProvider, sleeper: SleepRecorder
) -> None:
    fabric = make_client(router, tokens, sleeper, max_retries=2)
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-1", connect_error)
    with pytest.raises(FabricUnavailableError) as excinfo:
        await fabric.workspaces.get_workspace("ws-1")
    await fabric.aclose()
    assert excinfo.value.error_code == "TransportError"
    assert excinfo.value.retryable is True
    assert excinfo.value.correlation_id is not None
    assert len(router.requests) == 3
    assert len(sleeper.delays) == 2


async def test_transport_error_then_success(client: RestFabricClient, router: Router) -> None:
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-1", connect_error, respond(body={"id": "ws-1", "displayName": "A"}))
    assert (await client.workspaces.get_workspace("ws-1")).id == "ws-1"


async def test_404_maps_to_not_found_with_parsed_body(
    client: RestFabricClient, router: Router, sleeper: SleepRecorder
) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces/ws-1/items/missing",
        respond(
            404,
            {
                **fabric_error("ItemNotFound", "The requested item was not found", "req-404"),
                "moreDetails": [{"errorCode": "Detail", "message": "more"}],
            },
        ),
    )
    with pytest.raises(FabricNotFoundError) as excinfo:
        await client.items.get_item("ws-1", "missing")
    error = excinfo.value
    assert error.status_code == 404
    assert error.error_code == "ItemNotFound"
    assert error.request_id == "req-404"
    assert "The requested item was not found" in error.message
    assert error.details["more_details"] == [{"errorCode": "Detail", "message": "more"}]
    assert error.correlation_id == router.requests[0].headers[CLIENT_REQUEST_ID_HEADER]
    assert sleeper.delays == []  # not retried


@pytest.mark.parametrize("status", [401, 403])
async def test_401_and_403_map_to_auth_error(client: RestFabricClient, router: Router, status: int) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces",
        respond(
            status,
            fabric_error("InsufficientPrivileges", "No access", "req-auth"),
            headers={"RequestId": "req-auth-header"},
        ),
    )
    with pytest.raises(FabricAuthError) as excinfo:
        await client.workspaces.list_workspaces()
    assert excinfo.value.status_code == status
    assert excinfo.value.error_code == "InsufficientPrivileges"
    assert excinfo.value.request_id == "req-auth-header"
    assert len(router.requests) == 1


async def test_other_4xx_maps_to_base_fabric_error(client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET", f"{FABRIC_PATH}/workspaces/ws-1", respond(400, fabric_error("InvalidRequest", "Bad request", "req-400"))
    )
    with pytest.raises(FabricError) as excinfo:
        await client.workspaces.get_workspace("ws-1")
    assert type(excinfo.value) is FabricError
    assert excinfo.value.error_code == "InvalidRequest"
    assert excinfo.value.retryable is False


async def test_powerbi_error_body_is_parsed(client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET",
        f"{POWERBI_PATH}/groups/ws-1/datasets/sm-x/refreshes",
        respond(
            404,
            {"error": {"code": "ItemNotFound", "message": "Dataset sm-x is not found"}},
            headers={"RequestId": "pbi-req"},
        ),
    )
    with pytest.raises(FabricNotFoundError) as excinfo:
        await client.semantic_models.get_refresh_history("ws-1", "sm-x")
    assert excinfo.value.error_code == "ItemNotFound"
    assert excinfo.value.request_id == "pbi-req"
    assert "Dataset sm-x is not found" in excinfo.value.message


# ------------------------------------------------------------------------------ workspaces & items
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Sales Analytics [PROD]", Environment.PROD),
        ("sales-prod", Environment.PROD),
        ("Sales PROD", Environment.PROD),
        ("Finance Production", Environment.PROD),
        ("Sales Analytics [TEST]", Environment.TEST),
        ("finance-uat", Environment.TEST),
        ("Finance QA", Environment.TEST),
        ("Sales Pre-Prod", Environment.TEST),
        ("Sales Analytics [DEV]", Environment.DEV),
        ("marketing_dev", Environment.DEV),
        ("Contoso Non-PROD F16", Environment.UNKNOWN),
        ("Product Analytics", Environment.UNKNOWN),
        ("Shared", Environment.UNKNOWN),
        ("", Environment.UNKNOWN),
    ],
)
def test_infer_environment(name: str, expected: Environment) -> None:
    assert infer_environment(name) is expected


async def test_workspace_mapping(client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces",
        respond(
            body={
                "value": [
                    {
                        "id": "ws-1",
                        "displayName": "Sales Analytics [PROD]",
                        "capacityId": "cap-1",
                        "description": "Sales",
                        "type": "Workspace",
                    },
                    {"id": "ws-2", "displayName": "Sales Analytics-dev", "description": ""},
                ]
            }
        ),
    )
    prod, dev = await client.workspaces.list_workspaces()
    assert prod.tenant_id == "tenant-test"
    assert prod.display_name == "Sales Analytics [PROD]"
    assert prod.capacity_id == "cap-1"
    assert prod.description == "Sales"
    assert prod.environment is Environment.PROD
    assert prod.metadata == {"workspace_type": "Workspace"}
    assert dev.environment is Environment.DEV
    assert dev.capacity_id is None
    assert dev.description is None


async def test_item_type_mapping_and_inherited_environment(client: RestFabricClient, router: Router) -> None:
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-1", respond(body={"id": "ws-1", "displayName": "Sales [TEST]"}))
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces/ws-1/items",
        respond(
            body={
                "value": [
                    {
                        "id": "pl-1",
                        "type": "DataPipeline",
                        "displayName": "ingest",
                        "workspaceId": "ws-1",
                        "tags": [{"id": "t1", "displayName": "critical"}],
                    },
                    {"id": "nb-1", "type": "Notebook", "displayName": "transform"},
                    {"id": "lh-1", "type": "Lakehouse", "displayName": "bronze"},
                    {"id": "wh-1", "type": "Warehouse", "displayName": "dw"},
                    {"id": "sm-1", "type": "SemanticModel", "displayName": "Sales"},
                    {"id": "rp-1", "type": "Report", "displayName": "Sales report"},
                    {"id": "kql-1", "type": "KQLDatabase", "displayName": "events", "folderId": "f-1"},
                ]
            }
        ),
    )
    items = {i.id: i for i in await client.items.list_items("ws-1")}
    assert isinstance(items["pl-1"], Pipeline)
    assert isinstance(items["nb-1"], Notebook)
    assert isinstance(items["lh-1"], Lakehouse)
    assert isinstance(items["wh-1"], Warehouse)
    assert isinstance(items["sm-1"], SemanticModel)
    assert isinstance(items["rp-1"], Report)
    generic = items["kql-1"]
    assert isinstance(generic, GenericItem)
    assert generic.item_type is ItemType.OTHER
    assert generic.fabric_type == "KQLDatabase"
    assert generic.metadata == {"folder_id": "f-1"}
    assert items["pl-1"].tags == ["critical"]
    assert all(i.environment is Environment.TEST and i.workspace_id == "ws-1" for i in items.values())

    others = await client.items.list_items("ws-1", item_type=ItemType.OTHER)
    assert [i.id for i in others] == ["kql-1"]
    assert "type" not in router.calls("GET", f"{FABRIC_PATH}/workspaces/ws-1/items")[-1].url.params
    # The workspace environment is looked up once and cached.
    assert len(router.calls("GET", f"{FABRIC_PATH}/workspaces/ws-1")) == 1


async def test_logical_item_types_are_never_requested(client: RestFabricClient, router: Router) -> None:
    assert await client.items.list_items("ws-1", item_type=ItemType.DATA_PRODUCT) == []
    assert await client.items.list_items("ws-1", item_type=ItemType.EXTERNAL_SOURCE) == []
    assert router.requests == []


async def test_get_item_uses_cached_workspace_environment(client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET", f"{FABRIC_PATH}/workspaces", respond(body={"value": [{"id": "ws-1", "displayName": "Sales [PROD]"}]})
    )
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces/ws-1/items/nb-1",
        respond(body={"id": "nb-1", "type": "Notebook", "displayName": "transform", "workspaceId": "ws-1"}),
    )
    await client.workspaces.list_workspaces()
    item = await client.items.get_item("ws-1", "nb-1")
    assert isinstance(item, Notebook)
    assert item.environment is Environment.PROD
    assert router.calls("GET", f"{FABRIC_PATH}/workspaces/ws-1") == []


# --------------------------------------------------------------------------------------------- jobs
JOBS_PATH = f"{FABRIC_PATH}/workspaces/ws-1/items/pl-1/jobs/instances"


def job(job_id: str, status: str, start: str | None, end: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "itemId": "pl-1",
        "jobType": "Pipeline",
        "invokeType": "Scheduled",
        "status": status,
        "rootActivityId": f"root-{job_id}",
        "startTimeUtc": start,
        "endTimeUtc": end,
        "failureReason": None,
        **extra,
    }


async def test_job_instances_mapping_newest_first_and_limit(client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET",
        JOBS_PATH,
        respond(
            body={
                "value": [
                    job("job-1", "Completed", "2026-10-07T06:00:03.1234567", "2026-10-07T06:18:41.7654321"),
                    job(
                        "job-2",
                        "Failed",
                        "2026-10-08T06:00:03Z",
                        "2026-10-08T06:02:10Z",
                        failureReason={
                            "requestId": "req-fail",
                            "errorCode": "RequestExecutionFailed",
                            "message": "Activity transform_sales failed",
                        },
                    ),
                ]
            }
        ),
    )
    runs = await client.jobs.list_job_instances("ws-1", "pl-1")
    assert [r.id for r in runs] == ["job-2", "job-1"]
    failed, succeeded = runs
    assert succeeded.status is RunStatus.SUCCEEDED
    assert succeeded.failure is None
    assert succeeded.start_time == datetime(2026, 10, 7, 6, 0, 3, 123456, tzinfo=UTC)
    assert succeeded.end_time is not None and succeeded.end_time.tzinfo is not None
    assert failed.status is RunStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.error_code == "RequestExecutionFailed"
    assert failed.failure.message == "Activity transform_sales failed"
    assert failed.failure.request_id == "req-fail"
    assert failed.correlation_id == "root-job-2"
    assert failed.invoke_type == "Scheduled"
    assert failed.item_type is ItemType.DATA_PIPELINE
    assert failed.tenant_id == "tenant-test"
    assert failed.duration_seconds == 127.0

    limited = await client.jobs.list_job_instances("ws-1", "pl-1", limit=1)
    assert [r.id for r in limited] == ["job-2"]


@pytest.mark.parametrize(
    ("fabric_status", "expected"),
    [
        ("NotStarted", RunStatus.NOT_STARTED),
        ("InProgress", RunStatus.IN_PROGRESS),
        ("Completed", RunStatus.SUCCEEDED),
        ("Failed", RunStatus.FAILED),
        ("Cancelled", RunStatus.CANCELLED),
        ("Deduped", RunStatus.DEDUPED),
        ("SomethingNew", RunStatus.IN_PROGRESS),
    ],
)
async def test_job_status_mapping(
    client: RestFabricClient, router: Router, fabric_status: str, expected: RunStatus
) -> None:
    router.add("GET", f"{JOBS_PATH}/job-1", respond(body=job("job-1", fabric_status, "2026-10-08T06:00:00Z")))
    run = await client.jobs.get_job_instance("ws-1", "pl-1", "job-1")
    assert run.status is expected
    assert run.metadata["fabric_status"] == fabric_status


# ------------------------------------------------------------------------------------ activity runs
async def test_query_activity_runs_posts_read_only_query_and_maps_results(
    client: RestFabricClient, router: Router
) -> None:
    router.add(
        "GET",
        f"{JOBS_PATH}/job-9",
        respond(body=job("job-9", "Failed", "2026-10-08T06:00:03Z", "2026-10-08T06:20:00Z")),
    )
    query_path = f"{FABRIC_PATH}/workspaces/ws-1/datapipelines/pipelineruns/job-9/queryactivityruns"
    router.add(
        "POST",
        query_path,
        respond(
            body={
                "value": [
                    {
                        "activityName": "copy_crm",
                        "activityType": "Copy",
                        "activityRunId": "act-1",
                        "status": "Succeeded",
                        "activityRunStart": "2026-10-08T06:00:05.1234567Z",
                        "activityRunEnd": "2026-10-08T06:01:31Z",
                        "input": {"source": {"type": "SqlSource"}},
                        "output": {"rowsCopied": 10},
                        "durationInMs": 86000,
                        "error": {"errorCode": "", "message": "", "failureType": "", "target": "copy_crm"},
                    },
                    {
                        "activityName": "transform_sales",
                        "activityType": "TridentNotebook",
                        "activityRunId": "act-2",
                        "status": "Failed",
                        "activityRunStart": "2026-10-08T06:01:33Z",
                        "activityRunEnd": "2026-10-08T06:02:10Z",
                        "input": {"notebookId": "nb-1", "workspaceId": "ws-1"},
                        "output": "not-json",
                        "error": {
                            "errorCode": "2011",
                            "message": "Notebook failed: column customer_id missing",
                            "failureType": "UserError",
                        },
                    },
                    {
                        "activityName": "refresh_model",
                        "activityType": "PBISemanticModelRefresh",
                        "activityRunId": "act-3",
                        "status": "Skipped",
                        "input": json.dumps({"datasetId": "sm-1", "groupId": "ws-1"}),
                        "output": None,
                    },
                ]
            }
        ),
    )
    activities = await client.pipelines.query_activity_runs("ws-1", "pl-1", "job-9")

    (post,) = router.calls("POST", query_path)
    assert json.loads(post.content) == {
        "filters": [],
        "orderBy": [{"orderBy": "ActivityRunStart", "order": "ASC"}],
        "lastUpdatedAfter": "2026-10-08T05:00:03Z",
        "lastUpdatedBefore": "2026-10-08T07:20:00Z",
    }
    assert post.headers["Authorization"] == f"Bearer {TOKENS[FABRIC_SCOPE]}"

    copy, transform, refresh = activities
    assert copy.id == "act-1"
    assert copy.status is RunStatus.SUCCEEDED
    assert copy.error is None  # empty error payload means no error
    assert copy.target_item_id is None
    assert copy.output == {"rowsCopied": 10}
    assert copy.start_time == datetime(2026, 10, 8, 6, 0, 5, 123456, tzinfo=UTC)
    assert copy.pipeline_run_id == "job-9"
    assert copy.pipeline_item_id == "pl-1"
    assert copy.correlation_id == "root-job-9"
    assert transform.status is RunStatus.FAILED
    assert transform.target_item_id == "nb-1"
    assert transform.error is not None
    assert transform.error.error_code == "2011"
    assert transform.error.failure_type == "UserError"
    assert transform.error.message == "Notebook failed: column customer_id missing"
    assert transform.output == {"value": "not-json"}
    assert refresh.status is RunStatus.SKIPPED
    assert refresh.target_item_id == "sm-1"
    assert refresh.output == {}


async def test_query_activity_runs_window_uses_clock_for_running_job(client: RestFabricClient, router: Router) -> None:
    router.add("GET", f"{JOBS_PATH}/job-10", respond(body=job("job-10", "InProgress", "2026-10-09T11:00:00Z")))
    query_path = f"{FABRIC_PATH}/workspaces/ws-1/datapipelines/pipelineruns/job-10/queryactivityruns"
    router.add("POST", query_path, respond(body={"value": []}))
    assert await client.pipelines.query_activity_runs("ws-1", "pl-1", "job-10") == []
    body = json.loads(router.calls("POST", query_path)[0].content)
    assert body["lastUpdatedBefore"] == "2026-10-09T13:00:00Z"  # NOW + margin


@pytest.mark.parametrize(
    ("activity_input", "expected"),
    [
        ({"notebookId": "nb-1"}, "nb-1"),
        ({"semanticModelId": "sm-1"}, "sm-1"),
        ({"datasetId": "sm-2", "groupId": "ws"}, "sm-2"),
        ({"artifactId": "art-1"}, "art-1"),
        ({"typeProperties": {"notebookId": "nb-nested"}}, "nb-nested"),
        ({"source": {"type": "SqlSource"}}, None),
        (None, None),
        ("not json", None),
    ],
)
def test_target_item_id(activity_input: Any, expected: str | None) -> None:
    assert target_item_id(activity_input) == expected


# ---------------------------------------------------------------------------------------- refreshes
async def test_refresh_history_mapping(client: RestFabricClient, router: Router) -> None:
    exception = json.dumps(
        {
            "errorCode": "ModelRefreshFailed_CredentialsNotSpecified",
            "errorDescription": "Credentials are missing for the data source",
        }
    )
    router.add(
        "GET",
        f"{POWERBI_PATH}/groups/ws-1/datasets/sm-1/refreshes",
        respond(
            body={
                "value": [
                    {
                        "requestId": "req-r1",
                        "id": 101,
                        "refreshType": "Scheduled",
                        "status": "Completed",
                        "startTime": "2026-10-07T06:19:30Z",
                        "endTime": "2026-10-07T06:24:10Z",
                    },
                    {
                        "requestId": "req-r2",
                        "id": 102,
                        "refreshType": "ViaApi",
                        "status": "Failed",
                        "startTime": "2026-10-08T06:19:30Z",
                        "endTime": "2026-10-08T06:20:10Z",
                        "serviceExceptionJson": exception,
                    },
                    {
                        "requestId": "req-r3",
                        "id": 103,
                        "refreshType": "OnDemand",
                        "status": "Unknown",
                        "startTime": "2026-10-09T06:19:30Z",
                    },
                    {
                        "requestId": "req-r4",
                        "id": 104,
                        "refreshType": "Scheduled",
                        "status": "Disabled",
                        "startTime": "2026-10-06T06:19:30Z",
                        "endTime": "2026-10-06T06:19:31Z",
                    },
                ]
            }
        ),
    )
    refreshes = await client.semantic_models.get_refresh_history("ws-1", "sm-1", top=10)
    assert [r.id for r in refreshes] == ["req-r3", "req-r2", "req-r1", "req-r4"]  # newest first
    running, failed, succeeded, disabled = refreshes
    assert running.status is RunStatus.IN_PROGRESS
    assert running.end_time is None
    assert failed.status is RunStatus.FAILED
    assert failed.refresh_type == "ViaApi"
    assert failed.error is not None
    assert failed.error.error_code == "ModelRefreshFailed_CredentialsNotSpecified"
    assert failed.error.message == "Credentials are missing for the data source"
    assert failed.correlation_id == "req-r2"
    assert failed.metadata["refresh_id"] == 102
    assert failed.item_id == "sm-1"
    assert failed.workspace_id == "ws-1"
    assert succeeded.status is RunStatus.SUCCEEDED
    assert succeeded.error is None
    assert succeeded.duration_seconds == 280.0
    assert disabled.status is RunStatus.CANCELLED

    assert len(await client.semantic_models.get_refresh_history("ws-1", "sm-1", top=2)) == 2


async def test_refresh_error_from_non_json_service_exception(client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET",
        f"{POWERBI_PATH}/groups/ws-1/datasets/sm-1/refreshes",
        respond(
            body={
                "value": [
                    {
                        "requestId": "req-1",
                        "id": 1,
                        "status": "Failed",
                        "startTime": "2026-10-08T06:19:30Z",
                        "serviceExceptionJson": "Gateway is offline",
                    },
                ]
            }
        ),
    )
    (refresh,) = await client.semantic_models.get_refresh_history("ws-1", "sm-1")
    assert refresh.error is not None
    assert refresh.error.message == "Gateway is offline"
    assert refresh.error.error_code is None
    assert refresh.refresh_type == "Scheduled"


# --------------------------------------------------------------------------------------- capacities
async def test_capacities_mapping(client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/capacities",
        respond(
            body={
                "value": [
                    {
                        "id": "cap-1",
                        "displayName": "Contoso PROD F64",
                        "sku": "F64",
                        "region": "Australia East",
                        "state": "Active",
                    },
                ]
            }
        ),
    )
    (capacity,) = await client.capacities.list_capacities()
    assert capacity.id == "cap-1"
    assert capacity.sku == "F64"
    assert capacity.region == "Australia East"
    assert capacity.state == "Active"
    assert capacity.environment is Environment.PROD
    assert capacity.tenant_id == "tenant-test"


async def test_capacity_metrics_are_explicitly_unavailable(client: RestFabricClient, router: Router) -> None:
    with pytest.raises(FabricCapabilityUnavailableError) as excinfo:
        await client.capacities.get_capacity_metrics("cap-1", start=NOW, end=NOW)
    assert "Eventhouse" in excinfo.value.message
    assert excinfo.value.error_code == "CapacityMetricsUnavailable"
    assert router.requests == []


# -------------------------------------------------------------------------------------- deployments
def _deployment_routes(router: Router) -> None:
    dp = f"{FABRIC_PATH}/deploymentPipelines"
    router.add(
        "GET",
        dp,
        respond(
            body={
                "value": [
                    {"id": "dp-other", "displayName": "Other"},
                    {"id": "dp-sales", "displayName": "Sales"},
                ]
            }
        ),
    )
    router.add(
        "GET",
        f"{dp}/dp-other/stages",
        respond(
            body={
                "value": [
                    {"id": "st-o1", "order": 0, "displayName": "Development", "workspaceId": "ws-other"},
                    {"id": "st-o2", "order": 1, "displayName": "Production"},  # unassigned stage
                ]
            }
        ),
    )
    router.add(
        "GET",
        f"{dp}/dp-other/stages/st-o1/items",
        respond(
            body={
                "value": [
                    {"itemId": "x-1", "itemDisplayName": "unrelated", "itemType": "Notebook"},
                ]
            }
        ),
    )
    router.add(
        "GET",
        f"{dp}/dp-sales/stages",
        respond(
            body={
                "value": [  # deliberately out of order
                    {"id": "st-3", "order": 2, "displayName": "Stage C", "workspaceId": "ws-prod"},
                    {"id": "st-1", "order": 0, "displayName": "Stage A", "workspaceId": "ws-dev"},
                    {"id": "st-2", "order": 1, "displayName": "Stage B", "workspaceId": "ws-test"},
                ]
            }
        ),
    )
    router.add(
        "GET",
        f"{dp}/dp-sales/stages/st-1/items",
        respond(
            body={
                "value": [
                    {
                        "itemId": "nb-dev",
                        "itemDisplayName": "transform_sales",
                        "itemType": "Notebook",
                        "lastDeploymentTime": "2026-10-02T05:12:00Z",
                    },
                    {"itemId": "sm-dev", "itemDisplayName": "Sales", "itemType": "SemanticModel"},
                ]
            }
        ),
    )
    router.add(
        "GET",
        f"{dp}/dp-sales/stages/st-2/items",
        respond(
            body={
                "value": [
                    {
                        "itemId": "nb-test",
                        "itemDisplayName": "transform_sales",
                        "itemType": "Notebook",
                        "lastDeploymentTime": "2026-09-28T01:05:00Z",
                        "sourceItemId": "nb-dev",
                    },
                ]
            }
        ),
    )
    router.add(
        "GET",
        f"{dp}/dp-sales/stages/st-3/items",
        respond(
            body={
                "value": [
                    {
                        "itemId": "nb-prod",
                        "itemDisplayName": "transform_sales",
                        "itemType": "Notebook",
                        "lastDeploymentTime": "2026-09-28T06:40:00Z",
                        "sourceItemId": "nb-test",
                    },
                    {"itemId": "rp-prod", "itemDisplayName": "transform_sales", "itemType": "Report"},
                ]
            }
        ),
    )


async def test_deployment_states_across_stages(client: RestFabricClient, router: Router) -> None:
    _deployment_routes(router)
    states = await client.deployments.get_deployment_states("nb-prod")
    assert [(s.item_id, s.environment) for s in states] == [
        ("nb-dev", Environment.DEV),
        ("nb-test", Environment.TEST),
        ("nb-prod", Environment.PROD),
    ]
    assert {s.logical_item_id for s in states} == {"Notebook:transform_sales"}
    assert all(s.item_type is ItemType.NOTEBOOK for s in states)
    prod = states[-1]
    assert prod.workspace_id == "ws-prod"
    assert prod.deployed_at == datetime(2026, 9, 28, 6, 40, tzinfo=UTC)
    assert prod.commit_id is None and prod.config_hash is None
    assert prod.metadata["deployment_pipeline_id"] == "dp-sales"
    assert prod.metadata["stage_name"] == "Stage C"
    assert prod.metadata["source_item_id"] == "nb-test"
    assert router.calls("GET", f"{FABRIC_PATH}/deploymentPipelines/dp-other/stages/st-o2/items") == []

    # Pipeline and stage listings are cached; stage items are re-read for fresh deployment times.
    again = await client.deployments.get_deployment_states("nb-dev")
    assert [s.item_id for s in again] == ["nb-dev", "nb-test", "nb-prod"]
    assert len(router.calls("GET", f"{FABRIC_PATH}/deploymentPipelines")) == 1
    assert len(router.calls("GET", f"{FABRIC_PATH}/deploymentPipelines/dp-sales/stages")) == 1
    assert len(router.calls("GET", f"{FABRIC_PATH}/deploymentPipelines/dp-sales/stages/st-3/items")) == 2


async def test_deployment_states_for_unknown_item_is_empty(client: RestFabricClient, router: Router) -> None:
    _deployment_routes(router)
    assert await client.deployments.get_deployment_states("not-deployed") == []


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["Development", "Test", "Production"], [Environment.DEV, Environment.TEST, Environment.PROD]),
        (["A", "B", "C", "D"], [Environment.DEV, Environment.TEST, Environment.TEST, Environment.PROD]),
        (["A", "B"], [Environment.DEV, Environment.PROD]),
        (["Build", "UAT", "Live"], [Environment.DEV, Environment.TEST, Environment.PROD]),
        (["Only"], [Environment.UNKNOWN]),
    ],
)
def test_stage_environment(names: list[str], expected: list[Environment]) -> None:
    assert [stage_environment(n, i, len(names)) for i, n in enumerate(names)] == expected


# ------------------------------------------------------------------------------------- dependencies
async def test_dependencies_come_from_declared_contracts(
    router: Router, tokens: FakeTokenProvider, sleeper: SleepRecorder
) -> None:
    dependency = Dependency(
        id="dep-1",
        tenant_id="tenant-test",
        source_item_id="pl-1",
        target_item_id="nb-1",
        dependency_type=DependencyType.TRIGGERS,
    )
    fabric = make_client(router, tokens, sleeper, contracts=EstateContracts(dependencies=[dependency]))
    assert await fabric.dependencies.list_dependencies() == [dependency]
    await fabric.aclose()
    assert router.requests == []


# ------------------------------------------------------------------------------- logging hygiene
async def test_tokens_and_authorization_headers_never_reach_logs(
    client: RestFabricClient, router: Router, orqentis_logs: pytest.LogCaptureFixture
) -> None:
    router.add(
        "GET",
        f"{FABRIC_PATH}/workspaces",
        respond(429, headers={"Retry-After": "1"}),
        respond(body={"value": [], "continuationUri": f"{FABRIC}/workspaces?continuationToken=page-secret"}),
        respond(body={"value": []}),
    )
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-1", respond(403, fabric_error("Forbidden", "nope", "r")))
    router.add("GET", f"{POWERBI_PATH}/groups/ws-1/datasets/sm-1/refreshes", respond(body={"value": []}))
    await client.workspaces.list_workspaces()
    await client.semantic_models.get_refresh_history("ws-1", "sm-1")
    with pytest.raises(FabricAuthError) as excinfo:
        await client.workspaces.get_workspace("ws-1")

    records = [r for r in orqentis_logs.records if r.name.startswith("orqentis")]
    http_records = [r for r in records if r.getMessage() == "fabric http request"]
    assert len(http_records) == len(router.requests) == 5
    assert {r.outcome for r in http_records} == {"ok", "retry", "error"}  # type: ignore[attr-defined]
    for record in records:
        dumped = f"{record.getMessage()} {record.__dict__!r}"
        for secret in TOKENS.values():
            assert secret not in dumped
        assert "Bearer" not in dumped
        assert "authorization" not in {k.lower() for k in record.__dict__}
        assert "page-secret" not in dumped  # query strings are never logged
    sample = http_records[0].__dict__
    for key in ("http_method", "http_path", "status_code", "attempt", "latency_ms", "request_id", "correlation_id"):
        assert key in sample
    assert all(secret not in json.dumps(excinfo.value.to_dict()) for secret in TOKENS.values())


async def test_aclose_closes_token_provider(router: Router, tokens: FakeTokenProvider, sleeper: SleepRecorder) -> None:
    fabric = make_client(router, tokens, sleeper)
    await fabric.aclose()
    assert tokens.closed


# --------------------------------------------------------------------------------------- auth module
class FakeCredential:
    def __init__(self, *, expires_in: float, fail: bool = False) -> None:
        self.expires_in = expires_in
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []
        self.closed = False
        self.now = 1_000_000.0

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.calls.append(scopes)
        if self.fail:
            raise ClientAuthenticationError("no identity available")
        return AccessToken(f"token-{len(self.calls)}", int(self.now + self.expires_in))

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeCredential:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


async def test_token_provider_caches_until_five_minutes_before_expiry() -> None:
    credential = FakeCredential(expires_in=3600)
    provider = AzureIdentityTokenProvider(credential, clock=lambda: credential.now)
    assert await provider.get_token(FABRIC_SCOPE) == "token-1"
    assert await provider.get_token(FABRIC_SCOPE) == "token-1"
    assert await provider.get_token(POWERBI_SCOPE) == "token-2"  # cached per scope
    credential.now += 3600 - 299  # inside the 5-minute refresh margin
    assert await provider.get_token(FABRIC_SCOPE) == "token-3"
    assert credential.calls == [(FABRIC_SCOPE,), (POWERBI_SCOPE,), (FABRIC_SCOPE,)]
    await provider.close()
    assert credential.closed


async def test_token_provider_maps_credential_failures_to_auth_error() -> None:
    provider = AzureIdentityTokenProvider(FakeCredential(expires_in=3600, fail=True))
    with pytest.raises(FabricAuthError) as excinfo:
        await provider.get_token(FABRIC_SCOPE)
    assert excinfo.value.error_code == "TokenAcquisitionFailed"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (AuthMode.DEFAULT, "DefaultAzureCredential"),
        (AuthMode.MANAGED_IDENTITY, "ManagedIdentityCredential"),
        (AuthMode.WORKLOAD_IDENTITY, "WorkloadIdentityCredential"),
        (AuthMode.SERVICE_PRINCIPAL, "EnvironmentCredential"),
    ],
)
async def test_credential_factories_select_by_auth_mode(
    mode: AuthMode, expected: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    token_file = tmp_path / "federated-token"
    token_file.write_text("assertion", encoding="utf-8")
    monkeypatch.setenv("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000002")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token_file))
    settings = Settings(auth_mode=mode, managed_identity_client_id="00000000-0000-0000-0000-000000000003")
    async_credential = build_async_credential(settings)
    sync_credential = build_sync_credential(settings)
    try:
        assert type(async_credential).__name__ == expected
        assert type(async_credential).__module__.startswith("azure.identity.aio")
        assert type(sync_credential).__name__ == expected
        assert not type(sync_credential).__module__.startswith("azure.identity.aio")
    finally:
        await async_credential.close()
        close = getattr(sync_credential, "close", None)
        if callable(close):
            close()
