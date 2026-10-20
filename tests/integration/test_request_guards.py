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
from orqentis.api.middleware import BodyLimitMiddleware, RateLimits, RefusalAudit, TokenBucketLimiter
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
    middleware: BodyLimitMiddleware,
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
    middleware = BodyLimitMiddleware(drain_app, max_body_bytes=65_536)
    for value in (b"abc", b"-1", b"1.5"):
        status, _ = await run_middleware(middleware, headers=[(b"content-length", value)])
        assert status == 400, value


async def test_under_declared_body_is_stopped_while_it_is_read(tmp_path: Path) -> None:
    middleware = BodyLimitMiddleware(drain_app, max_body_bytes=1024)
    with pytest.raises(ApiError) as refused:
        await run_middleware(middleware, headers=[(b"content-length", b"10")], body=[b"a" * 600, b"b" * 600])
    assert refused.value.status_code == 413


async def test_non_http_scopes_pass_through(tmp_path: Path) -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = BodyLimitMiddleware(app, max_body_bytes=65_536)
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


def test_a_forged_principal_header_cannot_drain_anyones_budget(tmp_path: Path) -> None:
    """Regression: the limiter used to key on the raw header, including on paths Easy Auth does not authenticate."""
    victim = ea.header(ea.user_access_token("oid-victim", ["FabricOps.Operator"]))
    forged = ea.header([(ea.OID_URI, "oid-victim")])  # only the victim's object id
    settings = make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH, api_rate_limit_mutating_per_minute=3)
    with TestClient(create_app(settings)) as client:
        for path in ("/health", "/health/ready", "/nope", "/openapi.json", "/incidents"):
            for method in ("post", "put", "delete"):
                getattr(client, method)(path, headers=forged)
        for _ in range(10):
            assert client.post("/health", headers=forged).status_code == 405
        # The victim's whole budget is intact.
        assert [client.post(APPROVE, headers=victim).status_code for _ in range(3)] == [404, 404, 404]
        assert client.post(APPROVE, headers=victim).status_code == 429


def test_unauthenticated_requests_never_consume_a_budget(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH, api_rate_limit_mutating_per_minute=2)
    operator = ea.header(ea.user_access_token("oid-operator", ["FabricOps.Operator"]))
    with TestClient(create_app(settings)) as client:
        assert [client.post(APPROVE).status_code for _ in range(10)] == [401] * 10
        assert [client.post(APPROVE, headers={"X-MS-CLIENT-PRINCIPAL": "%%%"}).status_code for _ in range(5)] == [
            401
        ] * 5
        ambiguous = ea.header(ea.roles_without_scp_only("oid-operator", ["FabricOps.Operator"]))
        assert [client.post(APPROVE, headers=ambiguous).status_code for _ in range(5)] == [403] * 5
        assert [client.post(APPROVE, headers=operator).status_code for _ in range(2)] == [404, 404]
        assert client.post(APPROVE, headers=operator).status_code == 429


def test_refused_authenticated_callers_are_limited_too_so_they_cannot_flood_the_audit_log(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH, api_rate_limit_mutating_per_minute=2)
    automation = ea.header(ea.app_token("oid-scheduler", ["FabricOps.Automation"]))
    with TestClient(create_app(settings)) as client:
        assert [client.post(APPROVE, headers=automation).status_code for _ in range(4)] == [403, 403, 429, 429]


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


def test_token_bucket_keys_are_independent() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(1, clock=clock)
    assert limiter.acquire("a") == 0.0
    assert limiter.acquire("b") == 0.0
    assert limiter.acquire("a") > 0
    assert limiter.acquire("b") > 0


def test_a_flood_of_new_keys_cannot_reset_a_bucket_that_is_limiting() -> None:
    """Regression: FIFO eviction let 10k distinct keys evict a drained bucket, which then refilled to full."""
    clock = FakeClock()
    limiter = TokenBucketLimiter(2, clock=clock, max_keys=100)
    assert [limiter.acquire("victim") for _ in range(2)] == [0.0, 0.0]
    assert limiter.acquire("victim") > 0  # drained
    for index in range(1000):
        limiter.acquire(f"attacker-{index}")
        assert len(limiter._buckets) <= 101  # the cap plus the shared overflow bucket
    assert limiter.acquire("victim") > 0  # still limited: it was never evicted
    clock.now += 31
    assert limiter.acquire("victim") == 0.0  # and it refills normally


def test_new_keys_share_an_overflow_bucket_when_the_table_is_full_of_active_buckets() -> None:
    limiter = TokenBucketLimiter(2, clock=FakeClock(), max_keys=3)
    for key in ("a", "b", "c"):
        assert limiter.acquire(key) == 0.0  # three active buckets fill the table
    results = [limiter.acquire(f"new-{index}") for index in range(4)]
    assert results[:2] == [0.0, 0.0] and all(r > 0 for r in results[2:])  # one shared bucket of 2 tokens


def test_idle_buckets_are_evicted_to_make_room() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(1, clock=clock, max_keys=2)
    limiter.acquire("a")
    limiter.acquire("b")
    clock.now += 61  # both refilled completely
    assert limiter.acquire("c") == 0.0
    assert set(limiter._buckets) == {"c"}


def test_rate_limits_report_retry_after_from_the_refill_time_and_recover(tmp_path: Path) -> None:
    clock = FakeClock()
    limits = RateLimits(make_settings(tmp_path, api_rate_limit_mutating_per_minute=2), clock=clock)
    limits.check("mutating", "oid-1")
    limits.check("mutating", "oid-1")
    with pytest.raises(ApiError) as limited:
        limits.check("mutating", "oid-1")
    assert limited.value.status_code == 429 and limited.value.code == "RATE_LIMITED"
    assert limited.value.headers == {"Retry-After": "30"}
    limits.check("mutating", "oid-2")  # another principal
    limits.check("expensive", "oid-1")  # another request class
    clock.now += 30
    limits.check("mutating", "oid-1")


def test_disabled_rate_limits_never_refuse(tmp_path: Path) -> None:
    limits = RateLimits(make_settings(tmp_path, api_rate_limit_enabled=False, api_rate_limit_mutating_per_minute=1))
    for _ in range(50):
        limits.check("mutating", "oid-1")


# ---- refusal audit ----------------------------------------------------------------------------------------


def test_refusal_audit_writes_one_record_per_principal_and_reason_and_caps_the_total() -> None:
    clock = FakeClock()
    audit = RefusalAudit(window_seconds=60, max_per_window=3, clock=clock)
    assert audit.should_record("oid-1", "ROLE_REQUIRED") == (True, 0)
    assert audit.should_record("oid-1", "ROLE_REQUIRED") == (False, 0)  # repeat within the window
    assert audit.should_record("oid-1", "PRINCIPAL_NOT_HUMAN") == (True, 1)  # another reason; reports the skip
    assert audit.should_record("oid-2", "ROLE_REQUIRED") == (True, 0)
    assert audit.should_record("oid-3", "ROLE_REQUIRED") == (False, 0)  # over the window cap
    clock.now += 61
    assert audit.should_record("oid-3", "ROLE_REQUIRED") == (True, 1)  # the next record reports what was skipped
    assert audit.should_record("oid-1", "ROLE_REQUIRED") == (True, 0)
