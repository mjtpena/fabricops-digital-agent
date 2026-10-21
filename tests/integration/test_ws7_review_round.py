"""Regression tests for the WS7 review round: each one failed against the first version of the change.

1. a hostile Delta commit cannot break a collection run; 2. a schema change on a busy table is not missed; 3. refreshes
in a workspace without monitoring are reported as unavailable; 4. only known launcher invoke types suppress a job
incident; 5. faults are errors; 6. a monitoring database cannot name a model of another workspace; 7. a read is capped
even when the server ignores Range, and a watermark never passes a commit that was not processed; 8. a collector that
cannot observe still records a run; 9. the refresh history tool follows the configured source.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import RefreshSource
from orqentis.domain.common import Environment
from orqentis.domain.delta import DeltaCommitFile, DeltaCommitRef, DeltaTableRef
from orqentis.domain.estate import DeltaTableDeclaration, SemanticModel
from orqentis.domain.events import EventType
from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.domain.runs import RunStatus
from orqentis.fabric.errors import FabricCapabilityUnavailableError, FabricError
from orqentis.fabric.rest.http import FabricHttp
from orqentis.fabric.rest.onelake import RestDeltaLogService
from orqentis.storage.event_store import EventQuery
from orqentis.telemetry.collectors import (
    MonitoringRefreshCollector,
    SchemaChangeCollector,
    TelemetryIngestor,
)
from orqentis.telemetry.delta import parse_commit, parse_schema_string
from orqentis.telemetry.normalization import launched_by_item
from orqentis.tools.models import ToolStatus
from orqentis.tools.registry import ToolContext
from tests.conftest import make_settings
from tests.support.metrics import MetricsHarness
from tests.support.signals import WS, at, catalog, lakehouse, model, monitoring_database, refresh_run
from tests.unit.test_signal_collectors import AFTER, BEFORE, DECLARATION, FakeFabric, FakeMonitoring, meta

DEEP = 100_000  # a ~200 KB line: far under every size cap, far over the recursion limit


@pytest.fixture
async def container(tmp_path: Path) -> AsyncIterator[Container]:
    c = await build_container(make_settings(tmp_path), reset=True)
    try:
        yield c
    finally:
        await c.aclose()


class Logs:
    """Delta logs for several tables: ``{table: {version: (modified_at, [raw JSON line, ...])}}``."""

    def __init__(self, tables: dict[str, dict[int, tuple[datetime | None, list[str]]]]) -> None:
        self.tables = tables
        self.reads: list[tuple[str, int]] = []
        self.broken: dict[str, Exception] = {}

    async def list_commits(self, table: DeltaTableRef) -> list[DeltaCommitRef]:
        return [DeltaCommitRef(version=v, modified_at=m) for v, (m, _) in sorted(self.tables[table.table].items())]

    async def read_commit(self, table: DeltaTableRef, version: int) -> DeltaCommitFile:
        self.reads.append((table.table, version))
        if table.table in self.broken:
            raise self.broken[table.table]
        return DeltaCommitFile(version=version, lines=self.tables[table.table][version][1])


def lines(columns: list[tuple[str, str]], **info: Any) -> list[str]:
    return [json.dumps(action) for action in meta(columns, **info)]


def collector(logs: Logs, container: Container | None = None, **kwargs: Any) -> SchemaChangeCollector:
    tables = [DECLARATION, DeltaTableDeclaration(id="tbl-2", item_id="lh-1", table="orders")]
    return SchemaChangeCollector(
        FakeFabric(delta_logs=logs),  # type: ignore[arg-type]
        catalog(lakehouse(), tables=tables),
        **({"event_store": container.event_store} if container else {}),
        **kwargs,
    )


def ingestor(
    container: Container, *collectors: Any, harness: MetricsHarness | None = None, **kwargs: Any
) -> TelemetryIngestor:
    return TelemetryIngestor(
        list(collectors),
        container.event_store,
        container.clock,
        metrics=harness.metrics if harness else container.metrics,
        **kwargs,
    )


# ======================================================================== 1. a hostile commit cannot break collection
def test_deeply_nested_json_is_skipped_never_raised() -> None:
    deep_info = '{"commitInfo":{"operation":"WRITE","x":' + "[" * DEEP + "]" * DEEP + "}}"
    deep_schema = json.dumps({"metaData": {"schemaString": "[" * DEEP + "]" * DEEP}})
    good = json.dumps({"metaData": {"schemaString": json.dumps({"fields": [{"name": "a", "type": "string"}]})}})
    parsed = parse_commit(DeltaCommitFile(version=1, lines=[deep_info, deep_schema, good]))
    assert parsed.schema is not None and [c.name for c in parsed.schema.columns] == ["a"]  # the good line still counts
    assert parse_schema_string("[" * DEEP + "]" * DEEP) is None


async def test_a_hostile_commit_does_not_fail_the_run_and_the_watermark_advances(container: Container) -> None:
    now = container.clock.now()
    hostile = [
        '{"commitInfo":{"a":' + "[" * DEEP + "]" * DEEP + "}}",
        json.dumps({"metaData": {"schemaString": "[" * DEEP}}),
    ]
    logs = Logs(
        {
            "customers": {0: (now - timedelta(hours=2), lines(BEFORE)), 1: (now - timedelta(minutes=5), hostile)},
            "orders": {0: (now - timedelta(hours=2), lines(BEFORE)), 1: (now - timedelta(minutes=5), lines(AFTER))},
        }
    )
    result = await ingestor(container, collector(logs, container)).run()
    assert result.errors == {}  # the hostile table is not even an error: its lines were skipped
    assert {e.payload["table"] for e in result.new_events if e.event_type is EventType.SCHEMA_CHANGED} == {"orders"}
    assert set(await container.event_store.get_watermarks("delta-schema-changes")) == {
        "tbl-1",
        "tbl-2",
    }  # both moved on


async def test_an_unexpected_exception_in_one_scope_is_that_scope_s_error_only(container: Container) -> None:
    now = container.clock.now()
    logs = Logs(
        {
            "customers": {0: (now - timedelta(minutes=5), lines(BEFORE))},
            "orders": {0: (now - timedelta(hours=2), lines(BEFORE)), 1: (now - timedelta(minutes=5), lines(AFTER))},
        }
    )
    logs.broken["customers"] = KeyError("surprise")
    result = await ingestor(container, collector(logs, container)).run()
    assert (
        list(result.errors) == ["delta-schema-changes:tbl-1"]
        and "KeyError" in result.errors["delta-schema-changes:tbl-1"]
    )
    assert [e.payload["table"] for e in result.new_events if e.event_type is EventType.SCHEMA_CHANGED] == ["orders"]
    assert set(await container.event_store.get_watermarks("delta-schema-changes")) == {
        "tbl-2"
    }  # only the failed one waits


# ======================================================================== 2. busy tables keep their baseline
async def test_a_schema_change_after_many_appends_is_not_missed(container: Container) -> None:
    clock = container.clock
    t0 = clock.now()
    commits: dict[int, tuple[datetime | None, list[str]]] = {0: (t0 - timedelta(minutes=1), lines(BEFORE))}
    store = container.event_store
    ingest = ingestor(container, collector(Logs({"customers": commits, "orders": {}}), container))
    # (orders has no log entries: an empty table directory)
    first = await ingest.run()
    assert [e.event_type for e in first.new_events] == [EventType.SCHEMA_SNAPSHOT]  # the schema in force is remembered

    # 35 appends (no metaData: Delta writes it only when the schema changes), collected in between
    for v in range(1, 36):
        commits[v] = (t0 + timedelta(minutes=v), [json.dumps({"commitInfo": {"operation": "WRITE"}})])
    clock.set(t0 + timedelta(minutes=40))
    assert (await ingest.run()).new_events == []

    # much later the schema changes; the window holds only that commit and the 20 before it carry no schema
    commits[36] = (t0 + timedelta(hours=3), lines(AFTER))
    clock.set(t0 + timedelta(hours=3, minutes=5))
    changed = await ingest.run()
    events = [e for e in changed.new_events if e.event_type is EventType.SCHEMA_CHANGED]
    assert [(e.payload["previous_version"], e.payload["current_version"]) for e in events] == [(0, 36)]
    assert events[0].payload["removed_columns"] == ["customer_id"] and events[0].payload["added_columns"] == [
        "client_id"
    ]
    snapshots = await store.query(EventQuery(event_types=[EventType.SCHEMA_SNAPSHOT], correlation_id="tbl-1"))
    assert sorted(e.payload["version"] for e in snapshots) == [0, 36]


# ======================================================================== 3. no monitoring is visible, not silent
def _second_workspace(c: Any) -> None:
    from orqentis.domain.estate import Workspace

    c.workspaces["ws-2"] = Workspace(
        id="ws-2", tenant_id="tenant-test", display_name="Other [PROD]", environment=Environment.PROD
    )
    c.items["sm-2"] = SemanticModel(
        id="sm-2", tenant_id="tenant-test", workspace_id="ws-2", display_name="OtherModel", environment=Environment.PROD
    )


async def test_workspaces_with_models_but_no_monitoring_are_unavailable_not_invisible(container: Container) -> None:
    cat = catalog(model(), monitoring_database())
    _second_workspace(cat)
    cat.workspaces["ws-3"] = cat.workspaces["ws-2"].model_copy(
        update={"id": "ws-3"}
    )  # a workspace with no model at all
    monitoring = FakeMonitoring([refresh_run("ref-1", "sm-1", RunStatus.FAILED, at(11), at(11, 2), message="boom")])
    watch = MonitoringRefreshCollector(FakeFabric(monitoring=monitoring), cat, database_name="Monitoring KQL database")  # type: ignore[arg-type]
    assert watch.scopes() == [WS, "ws-2"]  # every workspace that holds a semantic model

    with pytest.raises(FabricCapabilityUnavailableError) as caught:
        await watch.collect_scope("ws-2", since=at(8), until=at(12))
    assert caught.value.error_code == "WorkspaceMonitoringNotEnabled"
    assert [call[0] for call in monitoring.calls] == []  # decided from the catalog: no API call

    harness = MetricsHarness()
    container.clock.set(at(12))
    result = await ingestor(container, watch, harness=harness).run()
    assert list(result.unavailable) == ["workspace-monitoring-refreshes:ws-2"] and result.errors == {}
    assert harness.total("orqentis.collector.unavailable", collector="workspace-monitoring-refreshes") == 1
    assert [e.event_type for e in result.new_events] == [EventType.REFRESH_FAILED]  # the monitored workspace works


# ======================================================================== 4. only known launchers suppress
@pytest.mark.parametrize(
    ("invoke_type", "launched"),
    [("Pipeline", True), ("pipeline", True), ("ReferenceRun", True), ("Notebook", True)],
)
def test_known_launchers_are_recognised(invoke_type: str, launched: bool) -> None:
    assert launched_by_item(invoke_type) is launched


@pytest.mark.parametrize("invoke_type", ["Manual", "Scheduled", "SomethingMicrosoftAddsNextYear", "", None, "Api"])
def test_anything_else_is_a_direct_run_and_the_coverage_check_does_the_deduplication(invoke_type: str | None) -> None:
    assert launched_by_item(invoke_type) is False


# ======================================================================== 5. faults are errors
def test_faults_are_errors_not_missing_capabilities() -> None:
    from orqentis.fabric.rest.capacity_metrics import _schema_mismatch

    mismatch = _schema_mismatch("series", ["CU %"])
    assert isinstance(mismatch, FabricError) and not isinstance(mismatch, FabricCapabilityUnavailableError)
    assert mismatch.error_code == "CapacityMetricsSchemaMismatch"


# ======================================================================== 6. a model of another workspace is not ours
async def test_a_monitoring_database_cannot_name_a_model_of_another_workspace(container: Container) -> None:
    cat = catalog(model(), monitoring_database())
    _second_workspace(cat)
    spoofed = [
        refresh_run("ref-own", "sm-1", RunStatus.FAILED, at(11), at(11, 2), message="boom"),
        refresh_run("ref-other-ws", "sm-2", RunStatus.FAILED, at(11), at(11, 3), message="pretends to be PROD"),
        refresh_run("ref-unknown", "sm-nope", RunStatus.FAILED, at(11), at(11, 4), message="x"),
        refresh_run("ref-nb", "nb-1", RunStatus.FAILED, at(11), at(11, 5), message="not a model"),
    ]
    cat.items["nb-1"] = cat.items.get("nb-1") or lakehouse("nb-1", "not a model")
    watch = MonitoringRefreshCollector(
        FakeFabric(monitoring=FakeMonitoring(spoofed)), cat, database_name="Monitoring KQL database"
    )  # type: ignore[arg-type]
    events = await watch.collect_scope(WS, since=at(8), until=at(12))
    assert [e.correlation_id for e in events] == ["ref-own"]


# ================================================== 7. capped reads, and no watermark past unread commits
class Body(httpx.AsyncByteStream):
    def __init__(self, total: int, chunk: int = 64 * 1024) -> None:
        self.total, self.chunk, self.sent = total, chunk, 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while self.sent < self.total:
            self.sent += self.chunk
            yield b'{"add":{"path":"x"}}\n' * (self.chunk // 21)


class Tokens:
    async def get_token(self, scope: str) -> str:
        return "token-not-real"


async def _no_sleep(_: float) -> None:
    return None


TABLE = DeltaTableRef(id="t", item_id="i1", workspace_id="w1", table="customers")


async def test_a_server_that_ignores_range_is_not_read_beyond_the_cap() -> None:
    body = Body(total=200 * 1024 * 1024)  # 200 MB it would happily send
    http = FabricHttp(
        Tokens(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=body)),
        max_retries=0,
        sleep=_no_sleep,
    )
    service = RestDeltaLogService(
        http, base_url="https://onelake.example.invalid", scope="s/.default", max_commit_bytes=4096
    )
    commit = await service.read_commit(TABLE, 1)
    assert commit.truncated
    assert body.sent <= 4096 + 2 * body.chunk  # a chunk or two past the cap, not the file


async def test_a_listing_that_hits_the_page_cap_is_an_error_not_the_oldest_entries() -> None:
    def forever(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"paths": [{"name": "i1/Tables/customers/_delta_log/00000000000000000001.json"}]},
            headers={
                "x-ms-continuation": f"more-{len(request.url.params)}-{request.url.params.get('continuation', '')}"
            },
        )

    http = FabricHttp(Tokens(), transport=httpx.MockTransport(forever), max_retries=0, sleep=_no_sleep)
    service = RestDeltaLogService(http, base_url="https://onelake.example.invalid", scope="s/.default")
    with pytest.raises(FabricError) as caught:
        await service.list_commits(TABLE)
    assert caught.value.error_code == "DeltaLogListingTruncated"


async def test_the_watermark_never_passes_a_commit_that_was_not_processed(container: Container) -> None:
    clock = container.clock
    t0 = clock.now()
    columns = [("a", "string")]
    commits: dict[int, tuple[datetime | None, list[str]]] = {0: (t0 - timedelta(hours=1), lines(columns))}
    for v in range(1, 13):  # twelve schema changes in the window, more than one run may read
        columns = [*columns, (f"c{v}", "string")]
        commits[v] = (t0 - timedelta(minutes=60 - v), lines(columns))
    work = ingestor(container, collector(Logs({"customers": commits, "orders": {}}), container, max_commits=5))
    seen: list[int] = []
    for _ in range(4):
        result = await work.run()
        seen += [e.payload["current_version"] for e in result.new_events if e.event_type is EventType.SCHEMA_CHANGED]
        clock.advance(timedelta(minutes=1))
    assert sorted(seen) == list(range(1, 13))  # nothing dropped, nothing twice
    marks = await container.event_store.get_watermarks("delta-schema-changes")
    assert marks["tbl-1"] > t0 - timedelta(minutes=1)  # caught up: the watermark reached the present only at the end


async def test_a_backlog_older_than_the_lookback_is_still_worked_off_and_is_visible(container: Container) -> None:
    """A table that commits faster than ``max_commits`` per run falls behind; the lookback must not skip it."""
    clock = container.clock
    t0 = clock.now()
    commits: dict[int, tuple[datetime | None, list[str]]] = {0: (t0 - timedelta(hours=2), lines(BEFORE))}
    for v in range(1, 60):
        commits[v] = (
            t0 - timedelta(hours=1) + timedelta(minutes=v),
            [json.dumps({"commitInfo": {"operation": "WRITE"}})],
        )
    commits[60] = (t0 - timedelta(minutes=5), lines(AFTER))  # the change comes last, behind 59 quiet commits
    logs = Logs({"customers": commits, "orders": {}})
    harness = MetricsHarness()
    work = ingestor(container, collector(logs, container, max_commits=5), harness=harness)

    await work.run()  # first sight: 5 of the 61 commits
    assert harness.gauge("orqentis.collector.backlog") == 56  # visible: 56 commits waiting
    clock.advance(timedelta(days=10))  # the collector was down: the backlog is now older than the 7 day lookback
    found: list[tuple[int, int]] = []
    for _ in range(14):
        result = await work.run()
        found += [
            (e.payload["previous_version"], e.payload["current_version"])
            for e in result.new_events
            if e.event_type is EventType.SCHEMA_CHANGED
        ]
        clock.advance(timedelta(minutes=5))
    # not skipped by the lookback floor; the schema before it was last recorded at version 59 (a checkpoint carries it)
    assert found == [(59, 60)]
    assert harness.gauge("orqentis.collector.backlog") == 0  # and the metric fell back when it caught up

    reads = len(logs.reads)
    await work.run()
    assert len(logs.reads) == reads  # caught up: nothing is read again


# ================================================== 8. a collector that cannot observe still records a run
async def test_an_unavailable_collector_records_a_run_and_a_duration_that_the_alert_does_not_read(
    container: Container,
) -> None:
    from tests.integration.test_capability_unavailable import Failing, forbidden

    harness = MetricsHarness()
    await ingestor(
        container,
        Failing("semantic-model-refreshes", forbidden()),
        expected_forbidden=("semantic-model-refreshes",),
        harness=harness,
    ).run()
    # the WS9 collector-errors alert reads collector.runs{outcome=error}: it must stay at zero
    assert harness.total("orqentis.collector.runs", collector="semantic-model-refreshes", outcome="error") == 0
    assert harness.total("orqentis.collector.runs", collector="semantic-model-refreshes", outcome="unavailable") == 1
    assert harness.histogram("orqentis.collector.duration", collector="semantic-model-refreshes")[0] == 1
    assert harness.total("orqentis.collector.unavailable", collector="semantic-model-refreshes") == 1


# ================================================== 9. the refresh history tool follows the source
@pytest.mark.parametrize(
    ("source", "expected"),
    [(RefreshSource.BOTH, "fabric.workspace-monitoring"), (RefreshSource.NONE, None)],
)
async def test_the_refresh_history_tool_never_calls_a_source_the_deployment_cannot_read(
    tmp_path: Path, source: RefreshSource, expected: str | None
) -> None:
    c = await build_container(make_settings(tmp_path, scenario="refresh_failure", refresh_source=source), reset=True)
    try:
        response = await c.registry.invoke(
            "get_semantic_model_refresh_history",
            {"semantic_model_id": "sm-marketing", "top": 5},
            ToolContext(principal=SYSTEM_PRINCIPAL, trace_id="t"),
        )
        if expected is None:
            assert response.status is not ToolStatus.SUCCESS
            assert any(e.code == "CAPABILITY_UNAVAILABLE" for e in response.errors)
        else:
            assert response.status is ToolStatus.SUCCESS and response.source == expected
    finally:
        await c.aclose()


def test_the_delta_log_caps_are_deployment_parameters() -> None:
    from tests.conftest import REPO

    bicep = (REPO / "infra" / "bicep" / "main.bicep").read_text(encoding="utf-8")
    for variable, parameter in (
        ("ORQENTIS_DELTA_LOG_MAX_COMMITS", "deltaLogMaxCommits"),
        ("ORQENTIS_DELTA_LOG_MAX_COMMIT_BYTES", "deltaLogMaxCommitBytes"),
    ):
        assert f"name: '{variable}'" in bicep and f"string({parameter})" in bicep, variable
        assert f"`{parameter}`" in (REPO / "infra" / "bicep" / "README.md").read_text(encoding="utf-8")
