"""Global Fabric call budget (concurrency + rate) and the bounded, watermark-filtered job-instance listing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from orqentis.config.clock import FixedClock
from orqentis.domain.runs import RunStatus
from orqentis.fabric.fixtures import EstateContracts
from orqentis.fabric.rest.auth import FABRIC_SCOPE, POWERBI_SCOPE
from orqentis.fabric.rest.budget import FabricCallBudget
from orqentis.fabric.rest.client import RestFabricClient
from orqentis.fabric.rest.http import FabricHttp
from tests.integration.test_fabric_rest_client import (
    FABRIC,
    JOBS_PATH,
    NOW,
    POWERBI,
    FakeTokenProvider,
    Router,
    SleepRecorder,
    job,
)


class FakeTime:
    """Monotonic time that only moves when somebody sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def budget_on(fake: FakeTime, **kwargs: float) -> FabricCallBudget:
    return FabricCallBudget(
        max_concurrency=int(kwargs.get("max_concurrency", 4)),
        rate_per_second=kwargs.get("rate_per_second", 1000.0),
        burst=int(kwargs.get("burst", 1000)),
        monotonic=fake.monotonic,
        sleep=fake.sleep,
    )


# ------------------------------------------------------------------------------------------ concurrency
async def test_the_budget_caps_concurrent_calls_across_fabric_and_power_bi() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)  # long enough for every other caller to pile up behind the limit
        in_flight -= 1
        return httpx.Response(200, json={"value": []})

    budget = FabricCallBudget(max_concurrency=3, rate_per_second=10_000, burst=10_000)
    http = FabricHttp(FakeTokenProvider(), transport=httpx.MockTransport(handler), budget=budget)
    try:
        urls = [(f"{FABRIC}/workspaces", FABRIC_SCOPE), (f"{POWERBI}/groups", POWERBI_SCOPE)] * 6
        await asyncio.gather(*(http.get_json(url, scope=scope) for url, scope in urls))
    finally:
        await http.aclose()

    assert peak == 3, "twelve callers, two APIs, one shared limit of three"
    assert budget.peak_in_flight == 3 and budget.in_flight == 0


async def test_without_a_budget_calls_are_not_limited() -> None:
    in_flight = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, json={"value": []})

    http = FabricHttp(FakeTokenProvider(), transport=httpx.MockTransport(handler))
    try:
        await asyncio.gather(*(http.get_json(f"{FABRIC}/workspaces", scope=FABRIC_SCOPE) for _ in range(8)))
    finally:
        await http.aclose()
    assert peak == 8


async def test_a_retry_pause_does_not_hold_a_concurrency_slot() -> None:
    budget = FabricCallBudget(max_concurrency=1, rate_per_second=10_000, burst=10_000)
    slots_held_while_sleeping: list[int] = []

    async def sleep(_: float) -> None:
        slots_held_while_sleeping.append(budget.in_flight)

    responses = iter([httpx.Response(503), httpx.Response(200, json={"value": []})])
    http = FabricHttp(
        FakeTokenProvider(), transport=httpx.MockTransport(lambda r: next(responses)), sleep=sleep, budget=budget
    )
    try:
        await http.get_json(f"{FABRIC}/workspaces", scope=FABRIC_SCOPE)
    finally:
        await http.aclose()
    assert slots_held_while_sleeping == [0]


# ------------------------------------------------------------------------------------------------- rate
async def test_the_token_bucket_allows_a_burst_then_the_steady_rate() -> None:
    fake = FakeTime()
    budget = budget_on(fake, rate_per_second=2, burst=2)
    for _ in range(2):
        async with budget.slot():
            pass
    assert fake.slept == []  # the burst is free
    for _ in range(4):
        async with budget.slot():
            pass
    assert fake.slept == pytest.approx([0.5] * 4)  # then one call per half second


async def test_penalizing_the_budget_pauses_every_caller_and_resumes_at_the_steady_rate() -> None:
    fake = FakeTime()
    budget = budget_on(fake, rate_per_second=2, burst=10)
    budget.penalize(5)
    async with budget.slot():
        pass
    assert fake.now == pytest.approx(1000.0 + 5.0 + 0.5)  # the pause, then no free burst afterwards
    budget.penalize(0)  # ignored
    async with budget.slot():
        pass
    assert fake.now == pytest.approx(1000.0 + 5.0 + 1.0)


class SpyBudget(FabricCallBudget):
    def __init__(self) -> None:
        super().__init__(max_concurrency=2, rate_per_second=10_000, burst=10_000)
        self.penalties: list[float] = []

    def penalize(self, seconds: float) -> None:
        self.penalties.append(seconds)
        super().penalize(seconds)


async def test_throttling_responses_pause_the_whole_budget() -> None:
    budget = SpyBudget()
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),  # server says wait 3 s
            httpx.Response(429),  # no hint: back off, and still tell everybody
            httpx.Response(500),  # a plain server error is not a rate-limit signal
            httpx.Response(503, headers={"Retry-After": "7"}),  # an explicit hint on any retryable status counts
            httpx.Response(200, json={"value": []}),
        ]
    )
    http = FabricHttp(
        FakeTokenProvider(),
        transport=httpx.MockTransport(lambda r: next(responses)),
        sleep=SleepRecorder(),
        jitter=lambda: 0.0,
        budget=budget,
        max_retries=5,
    )
    try:
        await http.get_json(f"{FABRIC}/workspaces", scope=FABRIC_SCOPE)
    finally:
        await http.aclose()
    # attempt 1: Retry-After 3; attempt 2: 429 without a hint -> backoff 2.0; attempt 3: 500 -> none; attempt 4: 7.
    assert budget.penalties == [3.0, 2.0, 7.0]


def test_invalid_budgets_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        FabricCallBudget(max_concurrency=0)
    with pytest.raises(ValueError, match="rate_per_second"):
        FabricCallBudget(rate_per_second=0)
    with pytest.raises(ValueError, match="burst"):
        FabricCallBudget(burst=0)


def test_from_settings_builds_the_budget_from_configuration(tmp_path: object) -> None:
    from orqentis.config.settings import Settings

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        fabric_mode="rest",
        tenant_id="tenant-test",
        fabric_max_concurrency=2,
        fabric_rate_per_second=3,
        fabric_rate_burst=4,
        fabric_job_instances_max_pages=7,
    )
    client = RestFabricClient.from_settings(settings, EstateContracts(), FakeTokenProvider())
    http = client.jobs._ctx.http
    assert http._budget is not None and http._budget.max_concurrency == 2
    assert client.jobs._max_pages == 7


# --------------------------------------------------------------------------------------- job listings
@pytest.fixture
async def paged_client(router: Router) -> AsyncIterator[RestFabricClient]:
    client = RestFabricClient(
        fabric_base_url=FABRIC,
        powerbi_base_url=POWERBI,
        token_provider=FakeTokenProvider(),
        contracts=EstateContracts(),
        tenant_id="tenant-test",
        transport=httpx.MockTransport(router),
        sleep=SleepRecorder(),
        clock=FixedClock(NOW),
        job_instances_max_pages=2,
    )
    yield client
    await client.aclose()


@pytest.fixture
def router() -> Router:
    return Router()


def page(*runs: dict[str, object], token: str | None = None) -> httpx.Response:
    body: dict[str, object] = {"value": list(runs)}
    if token:
        body["continuationToken"] = token
    return httpx.Response(200, json=body)


SINCE = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)


async def test_job_instances_older_than_the_watermark_are_dropped(
    paged_client: RestFabricClient, router: Router
) -> None:
    router.add(
        "GET",
        JOBS_PATH,
        lambda r: page(
            job("old", "Completed", "2026-10-07T06:00:00Z", "2026-10-07T06:10:00Z"),
            job("recent", "Completed", "2026-10-08T06:00:00Z", "2026-10-08T06:10:00Z"),
            # Started before the watermark but finished after it: it must not be lost.
            job("long-running", "Failed", "2026-10-07T20:00:00Z", "2026-10-08T01:00:00Z"),
        ),
    )
    runs = await paged_client.jobs.list_job_instances("ws-1", "pl-1", since=SINCE)
    assert [r.id for r in runs] == ["recent", "long-running"]
    everything = await paged_client.jobs.list_job_instances("ws-1", "pl-1")
    assert {r.id for r in everything} == {"old", "recent", "long-running"}  # no ``since``: unchanged behaviour


async def test_pagination_never_stops_early_because_fabric_documents_no_order(
    paged_client: RestFabricClient, router: Router
) -> None:
    """Page 1 holds only old runs, yet page 2 is still read: a recent run may be anywhere in the listing."""
    router.add(
        "GET",
        JOBS_PATH,
        lambda r: (
            page(job("old-1", "Completed", "2026-10-01T06:00:00Z", "2026-10-01T06:10:00Z"), token="t2")
            if "continuationToken" not in r.url.params
            else page(job("fresh", "Failed", "2026-10-09T06:00:00Z", "2026-10-09T06:10:00Z"))
        ),
    )
    runs = await paged_client.jobs.list_job_instances("ws-1", "pl-1", since=SINCE)
    assert [r.id for r in runs] == ["fresh"]
    assert len(router.calls("GET", JOBS_PATH)) == 2


async def test_pagination_is_capped_by_configuration(paged_client: RestFabricClient, router: Router) -> None:
    router.add(
        "GET",
        JOBS_PATH,
        lambda r: page(
            job(f"job-{len(router.calls('GET', JOBS_PATH))}", "Completed", "2026-10-09T06:00:00Z"),
            token=f"t{len(router.calls('GET', JOBS_PATH)) + 1}",
        ),
    )
    runs = await paged_client.jobs.list_job_instances("ws-1", "pl-1")
    assert len(router.calls("GET", JOBS_PATH)) == 2  # job_instances_max_pages=2, however many pages exist
    assert len(runs) == 2


async def test_unfinished_runs_are_kept_when_they_started_after_the_watermark(
    paged_client: RestFabricClient, router: Router
) -> None:
    router.add(
        "GET",
        JOBS_PATH,
        lambda r: page(
            job("running", "InProgress", "2026-10-09T06:00:00Z"),
            job("stale-running", "InProgress", "2026-10-01T06:00:00Z"),
        ),
    )
    runs = await paged_client.jobs.list_job_instances("ws-1", "pl-1", since=SINCE)
    assert [(r.id, r.status) for r in runs] == [("running", RunStatus.IN_PROGRESS)]
    assert NOW - timedelta(days=1) > SINCE  # sanity: the fixture dates are what the test thinks they are
