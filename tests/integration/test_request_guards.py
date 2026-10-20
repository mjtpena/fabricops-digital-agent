"""Request size limit (413) and per-principal rate limiting (429 + Retry-After)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.types import Message, Receive, Scope, Send

from orqentis.api.app import create_app
from orqentis.api.errors import ApiError
from orqentis.api.middleware import RequestGuardMiddleware, TokenBucketLimiter
from orqentis.config.settings import ApiAuthMode
from tests.conftest import make_settings
from tests.support import easy_auth as ea

pytestmark = pytest.mark.security

USER = {"X-Orqentis-User": "olivia@contoso.com", "X-Orqentis-Roles": "FabricOps.Operator"}
OTHER_USER = {"X-Orqentis-User": "victor@contoso.com", "X-Orqentis-Roles": "FabricOps.Operator"}
APPROVE = "/approvals/APR-none/approve"  # unknown id: a request that is processed answers 404


def client_for(tmp_path: Path, **overrides: object) -> TestClient:
    return TestClient(create_app(make_settings(tmp_path, **overrides)))


@pytest.fixture
def small_body_client(tmp_path: Path) -> Iterator[TestClient]:
    with client_for(tmp_path, api_max_body_bytes=1024) as client:
        yield client


# ---- body size ---------------------------------------------------------------------------------------------


def test_declared_oversized_body_gets_413(small_body_client: TestClient) -> None:
    response = small_body_client.post(APPROVE, headers=USER, json={"comment": "x" * 2000})
    assert response.status_code == 413
    assert response.json()["error"] == "PAYLOAD_TOO_LARGE"


def test_body_within_the_limit_is_processed(small_body_client: TestClient) -> None:
    response = small_body_client.post(APPROVE, headers=USER, json={"comment": "x" * 200})
    assert response.status_code == 404  # reached the handler: unknown approval


def test_chunked_oversized_body_gets_413_without_a_content_length(small_body_client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"comment": "'
        for _ in range(4):
            yield b"y" * 600
        yield b'"}'

    response = small_body_client.post(APPROVE, headers={**USER, "Content-Type": "application/json"}, content=chunks())
    assert response.status_code == 413
    assert response.json()["error"] == "PAYLOAD_TOO_LARGE"


def test_limit_applies_to_every_method_and_path(small_body_client: TestClient) -> None:
    assert small_body_client.post("/telemetry/collect", headers=USER, content=b"z" * 2000).status_code == 413
    assert small_body_client.put("/incidents", headers=USER, content=b"z" * 2000).status_code == 413


async def run_middleware(
    middleware: RequestGuardMiddleware,
    *,
    method: str = "POST",
    path: str = "/x",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: list[bytes] | None = None,
) -> tuple[int, dict[str, str]]:
    scope: Scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": ("203.0.113.9", 4242),
    }
    chunks = list(body or [b""])

    async def receive() -> Message:
        chunk = chunks.pop(0) if chunks else b""
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}


async def drain_app(scope: Scope, receive: Receive, send: Send) -> None:
    more = True
    while more:
        more = bool((await receive()).get("more_body"))
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_invalid_content_length_is_400(tmp_path: Path) -> None:
    middleware = RequestGuardMiddleware(drain_app, settings=make_settings(tmp_path))
    for value in (b"abc", b"-1", b"1.5"):
        status, _ = await run_middleware(middleware, headers=[(b"content-length", value)])
        assert status == 400, value


async def test_under_declared_body_is_stopped_while_it_is_read(tmp_path: Path) -> None:
    middleware = RequestGuardMiddleware(drain_app, settings=make_settings(tmp_path, api_max_body_bytes=1024))
    with pytest.raises(ApiError) as refused:
        await run_middleware(middleware, headers=[(b"content-length", b"10")], body=[b"a" * 600, b"b" * 600])
    assert refused.value.status_code == 413


async def test_non_http_scopes_pass_through(tmp_path: Path) -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = RequestGuardMiddleware(app, settings=make_settings(tmp_path))
    await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


# ---- rate limiting -----------------------------------------------------------------------------------------


def test_mutating_requests_are_rate_limited_per_principal_with_retry_after(tmp_path: Path) -> None:
    with client_for(tmp_path, api_rate_limit_mutating_per_minute=3) as client:
        assert [client.post(APPROVE, headers=USER).status_code for _ in range(3)] == [404, 404, 404]
        limited = client.post(APPROVE, headers=USER)
        assert limited.status_code == 429
        assert limited.json()["error"] == "RATE_LIMITED"
        retry_after = int(limited.headers["Retry-After"])
        assert 1 <= retry_after <= 20  # one token every 20 s at 3 per minute
        # Another principal has its own budget.
        assert client.post(APPROVE, headers=OTHER_USER).status_code == 404


def test_reads_are_not_rate_limited(tmp_path: Path) -> None:
    with client_for(tmp_path, api_rate_limit_mutating_per_minute=1, api_rate_limit_expensive_per_minute=1) as client:
        assert all(client.get("/incidents", headers=USER).status_code == 200 for _ in range(25))
        assert all(client.get("/health").status_code == 200 for _ in range(25))


def test_expensive_endpoints_have_their_own_smaller_budget(tmp_path: Path) -> None:
    with client_for(tmp_path, api_rate_limit_expensive_per_minute=2) as client:
        assert client.post("/telemetry/collect", headers=USER).status_code == 200
        assert client.post("/telemetry/collect", headers=USER).status_code == 200
        assert client.post("/telemetry/collect", headers=USER).status_code == 429
        # Investigations and audit verification share the expensive budget with collection.
        assert client.post("/incidents/INC-none/investigate", headers=USER).status_code == 429


def test_investigate_and_audit_verify_count_as_expensive(tmp_path: Path) -> None:
    with client_for(tmp_path, api_rate_limit_expensive_per_minute=2) as client:
        assert client.post("/incidents/INC-none/investigate", headers=USER).status_code == 404
        assert client.get("/audit/verify", headers=USER).status_code == 200
        assert client.post("/incidents/INC-none/investigate", headers=USER).status_code == 429
        assert client.get("/audit/verify", headers=USER).status_code == 429
        # Plain mutations are a different class and still allowed.
        assert client.post(APPROVE, headers=USER).status_code == 404


def test_limits_are_configurable_and_can_be_disabled(tmp_path: Path) -> None:
    with client_for(tmp_path, api_rate_limit_mutating_per_minute=500) as client:
        assert all(client.post(APPROVE, headers=USER).status_code == 404 for _ in range(60))
    with client_for(tmp_path, api_rate_limit_mutating_per_minute=1, api_rate_limit_enabled=False) as client:
        assert all(client.post(APPROVE, headers=USER).status_code == 404 for _ in range(10))


def test_easy_auth_rate_limit_keys_on_the_validated_principal_not_on_spoofable_headers(tmp_path: Path) -> None:
    operator = ea.header(ea.user_access_token("oid-operator", ["FabricOps.Operator"]))
    other = ea.header(ea.user_access_token("oid-other", ["FabricOps.Operator"]))
    settings = make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH, api_rate_limit_mutating_per_minute=2)
    with TestClient(create_app(settings)) as client:
        for index in range(2):
            spoofed = {**operator, "X-Orqentis-User": f"someone-{index}@contoso.com"}
            assert client.post(APPROVE, headers=spoofed).status_code == 404
        assert client.post(APPROVE, headers={**operator, "X-Orqentis-User": "fresh@contoso.com"}).status_code == 429
        assert client.post(APPROVE, headers=other).status_code == 404


def test_unauthenticated_callers_share_one_bucket_and_cannot_starve_others(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH, api_rate_limit_mutating_per_minute=2)
    operator = ea.header(ea.user_access_token("oid-operator", ["FabricOps.Operator"]))
    with TestClient(create_app(settings)) as client:
        assert [client.post(APPROVE).status_code for _ in range(2)] == [401, 401]
        assert client.post(APPROVE).status_code == 429
        assert client.post(APPROVE, headers={"X-MS-CLIENT-PRINCIPAL": "%%%"}).status_code == 429
        assert client.post(APPROVE, headers=operator).status_code == 404


def test_limit_settings_are_validated() -> None:
    for bad in (
        {"api_max_body_bytes": 10},
        {"api_rate_limit_mutating_per_minute": 0},
        {"api_rate_limit_expensive_per_minute": 0},
    ):
        with pytest.raises(ValidationError):
            make_settings(Path("/tmp"), **bad)


# ---- token bucket ------------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_token_bucket_bursts_then_refills_continuously() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(6, clock=clock)  # 6 per minute: one token every 10 s
    assert [limiter.acquire("a") for _ in range(6)] == [0.0] * 6
    wait = limiter.acquire("a")
    assert wait == pytest.approx(10.0)
    clock.now += 4
    assert limiter.acquire("a") == pytest.approx(6.0)  # a refused call does not delay the refill
    clock.now += 6
    assert limiter.acquire("a") == 0.0
    assert limiter.acquire("a") == pytest.approx(10.0)
    clock.now += 600
    assert [limiter.acquire("a") for _ in range(6)] == [0.0] * 6  # never more than the burst capacity
    assert limiter.acquire("a") > 0


def test_token_bucket_keys_are_independent_and_idle_buckets_are_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(1, clock=clock)
    assert limiter.acquire("a") == 0.0
    assert limiter.acquire("b") == 0.0
    assert limiter.acquire("a") > 0
    monkeypatch.setattr("orqentis.api.middleware._MAX_BUCKETS", 2)
    clock.now += 61
    assert limiter.acquire("c") == 0.0  # triggers pruning of the idle buckets a and b
    assert set(limiter._buckets) == {"c"}


def test_token_bucket_memory_is_bounded_under_a_flood_of_distinct_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orqentis.api.middleware._MAX_BUCKETS", 100)
    limiter = TokenBucketLimiter(5, clock=FakeClock())  # the clock never advances: nothing is idle
    for index in range(1000):
        assert limiter.acquire(f"principal:{index}") == 0.0
        assert len(limiter._buckets) <= 100


async def test_retry_after_matches_the_refill_time_and_recovers(tmp_path: Path) -> None:
    clock = FakeClock()
    settings = make_settings(tmp_path, api_rate_limit_mutating_per_minute=2)
    middleware = RequestGuardMiddleware(drain_app, settings=settings, clock=clock)
    headers = [(b"x-orqentis-user", b"olivia@contoso.com")]
    assert (await run_middleware(middleware, headers=headers))[0] == 200
    assert (await run_middleware(middleware, headers=headers))[0] == 200
    status, response_headers = await run_middleware(middleware, headers=headers)
    assert status == 429 and response_headers["retry-after"] == "30"
    clock.now += 30
    assert (await run_middleware(middleware, headers=headers))[0] == 200
