"""The WS7 collectors: item jobs, workspace-monitoring refreshes, Delta schema changes and capacity (ADR-0020)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orqentis.domain.common import Environment, ItemType
from orqentis.domain.delta import DeltaCommitFile, DeltaCommitRef, DeltaTableRef
from orqentis.domain.estate import DeltaTableDeclaration
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.runs import CapacitySnapshot, PipelineRun, Refresh, RunStatus
from orqentis.fabric.errors import FabricCapabilityUnavailableError
from orqentis.storage.event_store import EventQuery, LocalEventStore
from orqentis.storage.sqlite import SqliteDatabase
from orqentis.telemetry.collectors import (
    CapacityCollector,
    ItemJobCollector,
    MonitoringRefreshCollector,
    SchemaChangeCollector,
)
from tests.support.signals import (
    NOW,
    WS,
    at,
    catalog,
    dataflow,
    job_run,
    lakehouse,
    model,
    monitoring_database,
    notebook,
    refresh_run,
    spark_job,
)


class FakeJobs:
    def __init__(self, runs: list[PipelineRun]) -> None:
        self.runs = runs
        self.calls: list[tuple[str, str, datetime | None]] = []

    async def list_job_instances(
        self, workspace_id: str, item_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[PipelineRun]:
        self.calls.append((workspace_id, item_id, since))
        return [r for r in self.runs if r.item_id == item_id][:limit]


class FakeMonitoring:
    def __init__(self, refreshes: list[Refresh]) -> None:
        self.refreshes = refreshes
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def list_semantic_model_refreshes(
        self, workspace_id: str, *, since: datetime, until: datetime
    ) -> list[Refresh]:
        self.calls.append((workspace_id, since, until))
        return self.refreshes


class FakeDeltaLogs:
    """A table's log as ``{version: (modified_at, [action, ...])}``."""

    def __init__(self, commits: dict[int, tuple[datetime | None, list[dict[str, Any]]]]) -> None:
        self.commits = commits
        self.reads: list[int] = []

    async def list_commits(self, table: DeltaTableRef) -> list[DeltaCommitRef]:
        return [DeltaCommitRef(version=v, modified_at=m) for v, (m, _) in sorted(self.commits.items())]

    async def read_commit(self, table: DeltaTableRef, version: int) -> DeltaCommitFile:
        self.reads.append(version)
        return DeltaCommitFile(version=version, lines=[json.dumps(a) for a in self.commits[version][1]])


class FakeCapacities:
    def __init__(self) -> None:
        self.windows: list[tuple[str, datetime, datetime]] = []

    async def get_capacity_metrics(self, capacity_id: str, *, start: datetime, end: datetime) -> list[CapacitySnapshot]:
        self.windows.append((capacity_id, start, end))
        return [
            CapacitySnapshot(
                capacity_id=capacity_id, timestamp=end, cu_utilization_percent=120.0, interactive_delay_percent=12.0
            )
        ]


class FakeFabric:
    def __init__(self, **services: Any) -> None:
        for name, service in services.items():
            setattr(self, name, service)


# ---------------------------------------------------------------------------------------------- item jobs
async def test_notebook_runs_become_events_for_finished_runs_only() -> None:
    runs = [
        job_run("r-ok", "nb-1", RunStatus.SUCCEEDED, at(10), at(10, 5)),
        job_run("r-bad", "nb-1", RunStatus.FAILED, at(11), at(11, 3), message="ValueError: boom", code="200"),
        job_run("r-going", "nb-1", RunStatus.IN_PROGRESS, at(11, 50), at(11, 55)),
        job_run("r-cancel", "nb-1", RunStatus.CANCELLED, at(9), at(9, 1)),
        job_run("r-old", "nb-1", RunStatus.FAILED, at(1), at(1, 1), message="old"),
    ]
    jobs = FakeJobs(runs)
    collector = ItemJobCollector(FakeFabric(jobs=jobs), catalog(notebook(), lakehouse()), ItemType.NOTEBOOK)  # type: ignore[arg-type]
    assert collector.name == "notebook-runs" and collector.scopes() == ["nb-1"]

    events = await collector.collect_scope("nb-1", since=at(8), until=NOW)
    by_id = {e.correlation_id: e for e in events}
    assert set(by_id) == {"r-ok", "r-bad", "r-cancel"}  # in progress is not finished; r-old is before the window
    failed = by_id["r-bad"]
    assert failed.event_type is EventType.NOTEBOOK_RUN_FAILED and failed.item_type is ItemType.NOTEBOOK
    assert failed.payload["error_message"] == "ValueError: boom" and failed.payload["error_code"] == "200"
    assert failed.payload["launched_by_item"] is False
    assert failed.dedupe_key == "notebook-run:r-bad:Failed" and failed.environment is Environment.PROD
    assert by_id["r-ok"].event_type is EventType.NOTEBOOK_RUN_SUCCEEDED
    assert jobs.calls == [(WS, "nb-1", at(8))]  # the watermark window is handed to the service


@pytest.mark.parametrize(
    ("item_type", "item", "name", "failed", "succeeded"),
    [
        (
            ItemType.SPARK_JOB_DEFINITION,
            spark_job(),
            "spark-job-runs",
            EventType.SPARK_JOB_FAILED,
            EventType.SPARK_JOB_SUCCEEDED,
        ),
        (
            ItemType.DATAFLOW,
            dataflow(),
            "dataflow-runs",
            EventType.DATAFLOW_REFRESH_FAILED,
            EventType.DATAFLOW_REFRESH_SUCCEEDED,
        ),
    ],
)
async def test_each_item_type_has_its_own_collector_and_event_types(
    item_type: ItemType, item: Any, name: str, failed: EventType, succeeded: EventType
) -> None:
    runs = [
        job_run("a", item.id, RunStatus.FAILED, at(10), at(10, 2), item_type=item_type, message="boom"),
        job_run("b", item.id, RunStatus.SUCCEEDED, at(9), at(9, 2), item_type=item_type),
    ]
    collector = ItemJobCollector(FakeFabric(jobs=FakeJobs(runs)), catalog(item, notebook()), item_type)  # type: ignore[arg-type]
    assert collector.name == name and collector.scopes() == [item.id]  # the notebook is not its scope
    events = await collector.collect(since=at(0), until=NOW)
    assert {e.event_type for e in events} == {failed, succeeded}
    assert all(e.item_type is item_type for e in events)


async def test_a_job_another_item_started_is_marked_as_such() -> None:
    runs = [
        job_run("direct", "nb-1", RunStatus.FAILED, at(10), at(10, 1), invoke="Scheduled", message="x"),
        job_run("by-pipeline", "nb-1", RunStatus.FAILED, at(11), at(11, 1), invoke="Pipeline", message="x"),
        job_run("unknown", "nb-1", RunStatus.FAILED, at(11, 30), at(11, 31), invoke=None, message="x"),
    ]
    collector = ItemJobCollector(FakeFabric(jobs=FakeJobs(runs)), catalog(notebook()), ItemType.NOTEBOOK)  # type: ignore[arg-type]
    flags = {e.correlation_id: e.payload["launched_by_item"] for e in await collector.collect(since=at(0), until=NOW)}
    assert flags == {"direct": False, "by-pipeline": True, "unknown": False}


async def test_a_scope_that_left_the_estate_yields_nothing_and_pipelines_have_no_job_collector() -> None:
    collector = ItemJobCollector(FakeFabric(jobs=FakeJobs([])), catalog(notebook()), ItemType.NOTEBOOK)  # type: ignore[arg-type]
    assert await collector.collect_scope("gone", since=at(0), until=NOW) == []
    with pytest.raises(ValueError, match="no job collector"):
        ItemJobCollector(FakeFabric(), catalog(), ItemType.DATA_PIPELINE)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------- workspace monitoring
async def test_monitoring_refreshes_are_collected_from_workspaces_that_have_a_monitoring_database() -> None:
    other = monitoring_database("Some other KQL database")
    other = other.model_copy(update={"id": "kql-2", "workspace_id": "ws-2"})
    monitoring = FakeMonitoring(
        [
            refresh_run("ref-1", "sm-1", RunStatus.FAILED, at(11), at(11, 2), message="cannot access the source"),
            refresh_run("ref-2", "sm-1", RunStatus.SUCCEEDED, at(9), at(9, 2)),
            refresh_run("ref-3", "sm-9", RunStatus.IN_PROGRESS, at(11, 50), at(11, 55)),
            refresh_run("ref-4", "sm-unknown", RunStatus.FAILED, at(11, 10), at(11, 12), message="x"),
        ]
    )
    collector = MonitoringRefreshCollector(
        FakeFabric(monitoring=monitoring),  # type: ignore[arg-type]
        catalog(model(), monitoring_database("monitoring kql DATABASE"), other),
        database_name="Monitoring KQL database",
    )
    assert collector.name == "workspace-monitoring-refreshes"
    assert collector.scopes() == [WS]  # case-insensitive match; the other workspace's database has another name

    events = await collector.collect_scope(WS, since=at(8), until=NOW)
    # in-flight refreshes wait for their end; ref-4 names an id that is no semantic model of this workspace: not used
    assert {e.correlation_id for e in events} == {"ref-1", "ref-2"}
    failed = next(e for e in events if e.correlation_id == "ref-1")
    assert (
        failed.event_type is EventType.REFRESH_FAILED
        and failed.item_id == "sm-1"
        and failed.display_name == "SalesModel"
    )
    assert failed.payload["error_message"] == "cannot access the source"
    assert failed.environment is Environment.PROD  # the model's, from the estate
    # the query reaches back a little further than the window: events arrive in the monitoring database late
    assert monitoring.calls == [(WS, at(8) - MonitoringRefreshCollector.LATENCY_MARGIN, NOW)]


async def test_a_workspace_with_models_and_no_monitoring_database_is_unavailable_and_costs_no_call() -> None:
    monitoring = FakeMonitoring([])
    collector = MonitoringRefreshCollector(
        FakeFabric(monitoring=monitoring),  # type: ignore[arg-type]
        catalog(model(), notebook()),
        database_name="Monitoring KQL database",
    )
    assert collector.scopes() == [WS]  # every workspace that holds a semantic model is a scope
    with pytest.raises(FabricCapabilityUnavailableError):
        await collector.collect_scope(WS, since=at(8), until=NOW)
    assert monitoring.calls == []
    assert MonitoringRefreshCollector(FakeFabric(), catalog(notebook()), database_name="x").scopes() == []  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------ schema changes
def meta(columns: list[tuple[str, str]], **info: Any) -> list[dict[str, Any]]:
    schema = {
        "type": "struct",
        "fields": [{"name": n, "type": t, "nullable": True, "metadata": {}} for n, t in columns],
    }
    return [
        {"commitInfo": {"timestamp": int(info.pop("ts", 1_780_000_000_000)), "operation": "WRITE", **info}},
        {"metaData": {"schemaString": json.dumps(schema)}},
    ]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    db = SqliteDatabase(tmp_path / "events.sqlite3")
    yield LocalEventStore(db)
    db.close()


def changes(events: list[OperationalEvent]) -> list[OperationalEvent]:
    return [e for e in events if e.event_type is EventType.SCHEMA_CHANGED]


def snapshots(events: list[OperationalEvent]) -> list[OperationalEvent]:
    return [e for e in events if e.event_type is EventType.SCHEMA_SNAPSHOT]


DECLARATION = DeltaTableDeclaration(id="tbl-1", item_id="lh-1", table="customers")
BEFORE = [("customer_id", "string"), ("name", "string")]
AFTER = [("client_id", "string"), ("name", "string")]


def schema_collector(logs: FakeDeltaLogs, store: LocalEventStore, **kwargs: Any) -> SchemaChangeCollector:
    return SchemaChangeCollector(
        FakeFabric(delta_logs=logs),  # type: ignore[arg-type]
        catalog(
            lakehouse(), notebook(), tables=[DECLARATION, DeltaTableDeclaration(id="tbl-x", item_id="nb-1", table="t")]
        ),
        store,
        **kwargs,
    )


async def test_a_schema_change_in_a_recent_commit_becomes_one_event(store: LocalEventStore) -> None:
    logs = FakeDeltaLogs(
        {
            0: (at(1), meta(BEFORE)),
            1: (at(2), [{"commitInfo": {"operation": "WRITE"}}, {"add": {"path": "p"}}]),  # no metaData: no schema
            2: (at(11, 30), meta(AFTER, userMetadata="nightly load")),
        }
    )
    collector = schema_collector(logs, store)
    assert collector.name == "delta-schema-changes" and collector.scopes() == ["tbl-1"]  # only lakehouse tables
    events = await collector.collect_scope("tbl-1", since=at(10), until=NOW)

    assert len(changes(events)) == 1
    # the schema in force before (version 0, found by reading back) and the one that came (version 2) are remembered
    assert [s.payload["version"] for s in snapshots(events)] == [0, 2]
    event = changes(events)[0]
    assert (
        event.event_type is EventType.SCHEMA_CHANGED
        and event.item_id == "lh-1"
        and event.item_type is ItemType.LAKEHOUSE
    )
    assert event.payload["removed_columns"] == ["customer_id"] and event.payload["added_columns"] == ["client_id"]
    assert (event.payload["previous_version"], event.payload["current_version"]) == (0, 2)  # version 1 had no schema
    assert event.payload["table"] == "customers" and event.payload["user_metadata"] == "nightly load"
    assert event.dedupe_key == "schema:lh-1::customers:2" and event.source == "orqentis.collectors.delta-log"
    assert event.timestamp <= NOW and event.severity.value == "WARNING"
    # the baseline is found by reading back from the newest earlier commit (1 has no schema, 0 does)
    assert logs.reads == [1, 0, 2]


async def test_nothing_new_since_the_watermark_only_learns_the_schema_in_force(store: LocalEventStore) -> None:
    logs = FakeDeltaLogs({0: (at(1), meta(BEFORE)), 1: (at(2), meta(AFTER))})
    events = await schema_collector(logs, store).collect_scope("tbl-1", since=at(10), until=NOW)
    # a table seen for the first time is remembered even when it is quiet, so a later change has a "before" ...
    assert changes(events) == [] and [s.payload["version"] for s in snapshots(events)] == [1]
    assert logs.reads == [1]  # ... and that costs one read, the newest commit
    await store.append(events)
    logs.reads.clear()
    assert await schema_collector(logs, store).collect_scope("tbl-1", since=at(10), until=NOW) == []
    assert logs.reads == []  # known now: nothing to read until a commit arrives


async def test_a_rewritten_but_identical_schema_is_not_an_event(store: LocalEventStore) -> None:
    logs = FakeDeltaLogs({0: (at(1), meta(BEFORE)), 1: (at(11), meta(BEFORE))})
    events = await schema_collector(logs, store).collect_scope("tbl-1", since=at(10), until=NOW)
    assert changes(events) == [] and [s.payload["version"] for s in snapshots(events)] == [0]  # nothing new to remember


async def test_without_a_baseline_a_first_schema_is_not_a_change(store: LocalEventStore) -> None:
    logs = FakeDeltaLogs({5: (at(11), meta(AFTER))})
    events = await schema_collector(logs, store).collect_scope("tbl-1", since=at(10), until=NOW)
    assert changes(events) == [] and [s.payload["version"] for s in snapshots(events)] == [5]
    # ... and the commit after it is compared with it
    logs = FakeDeltaLogs({5: (at(11), meta(BEFORE)), 6: (at(11, 30), meta(AFTER))})
    again = await schema_collector(logs, store).collect_scope("tbl-1", since=at(10), until=NOW)
    assert len(changes(again)) == 1


async def test_several_changes_in_one_window_are_each_reported_in_order(store: LocalEventStore) -> None:
    third = [("client_id", "string"), ("name", "string"), ("segment", "string")]
    logs = FakeDeltaLogs({0: (at(1), meta(BEFORE)), 1: (at(11), meta(AFTER)), 2: (at(11, 30), meta(third))})
    events = changes(await schema_collector(logs, store).collect_scope("tbl-1", since=at(10), until=NOW))
    assert [(e.payload["previous_version"], e.payload["current_version"]) for e in events] == [(0, 1), (1, 2)]
    assert events[1].payload["added_columns"] == ["segment"] and events[1].severity.value == "INFO"


async def test_the_oldest_commits_are_read_first_and_the_rest_wait_for_the_next_run(store: LocalEventStore) -> None:
    commits: dict[int, tuple[datetime | None, list[dict[str, Any]]]] = {
        v: (at(11, v), meta(BEFORE if v % 2 else AFTER)) for v in range(10)
    }
    logs = FakeDeltaLogs(commits)
    collector = schema_collector(logs, store, max_commits=3, max_backscan=2)
    events = await collector.collect_scope("tbl-1", since=at(0), until=NOW)
    assert logs.reads == [0, 1, 2]  # the oldest three, not the newest: the others are not skipped, they come next
    assert len(changes(events)) == 2  # 0 -> 1 and 1 -> 2 (version 0 is the first schema seen)
    assert collector.progress("tbl-1") == at(11, 2)  # the watermark may move this far and no further
    assert collector.progress("tbl-1") is None  # consumed

    await store.append(events)
    logs.reads.clear()
    more = await collector.collect_scope("tbl-1", since=at(0), until=NOW)
    assert logs.reads == [3, 4, 5]  # continues after the last commit processed
    assert [e.payload["previous_version"] for e in changes(more)] == [2, 3, 4]
    assert collector.progress("tbl-1") == at(11, 5)

    await store.append(more)
    for _ in range(2):  # 6-8, then the last one
        await store.append(await collector.collect_scope("tbl-1", since=at(0), until=NOW))
    assert collector.progress("tbl-1") is None  # the window was read to its end
    assert await collector.collect_scope("tbl-1", since=at(0), until=NOW) == []


async def test_the_back_scan_is_bounded_and_the_arguments_are_checked(store: LocalEventStore) -> None:
    no_scan = FakeDeltaLogs({0: (at(1), meta(BEFORE)), 1: (at(11), meta(AFTER))})
    events = await schema_collector(no_scan, store, max_backscan=0).collect_scope("tbl-1", since=at(10), until=NOW)
    assert changes(events) == []  # no "before" was looked for, so the first schema read is only remembered
    with pytest.raises(ValueError, match="max_commits"):
        schema_collector(no_scan, store, max_commits=0)


async def test_a_checkpoint_records_how_far_a_run_got_when_no_commit_had_a_schema(store: LocalEventStore) -> None:
    quiet = [{"commitInfo": {"operation": "WRITE"}}]
    logs = FakeDeltaLogs({v: (at(11, v), quiet) for v in range(6)})
    collector = schema_collector(logs, store, max_commits=2)
    first = await collector.collect_scope("tbl-1", since=at(0), until=NOW)
    (checkpoint,) = snapshots(first)
    assert checkpoint.payload["checkpoint"] is True and checkpoint.payload["version"] == 1
    assert checkpoint.payload["columns"] is None  # no schema was ever seen: only the position is recorded
    await store.append(first)
    logs.reads.clear()
    await collector.collect_scope("tbl-1", since=at(0), until=NOW)
    assert logs.reads == [2, 3]  # not [0, 1] again


async def test_a_commit_without_a_modified_time_is_read(store: LocalEventStore) -> None:
    logs = FakeDeltaLogs({0: (None, meta(BEFORE)), 1: (None, meta(AFTER, ts=1_780_000_100_000))})
    events = await schema_collector(logs, store).collect_scope("tbl-1", since=at(10), until=NOW + timedelta(days=4000))
    assert len(changes(events)) == 1


async def test_a_log_that_starts_over_is_read_from_its_first_commit(store: LocalEventStore) -> None:
    logs = FakeDeltaLogs({v: (at(11, v), meta(BEFORE if v % 2 else AFTER)) for v in range(4)})
    collector = schema_collector(logs, store)
    await store.append(await collector.collect_scope("tbl-1", since=at(0), until=NOW))  # recorded up to version 3
    # the table was dropped and created again: its log is shorter than what was recorded
    logs.commits = {0: (at(11, 30), meta([("another_key", "long")]))}
    logs.reads.clear()
    events = await collector.collect_scope("tbl-1", since=at(0), until=NOW)
    assert logs.reads == [0] and len(changes(events)) == 1  # compared with the schema recorded before
    assert changes(events)[0].payload["added_columns"] == ["another_key"]


async def test_an_unreadable_snapshot_is_ignored_not_trusted(store: LocalEventStore) -> None:
    logs = FakeDeltaLogs({0: (at(1), meta(BEFORE))})
    collector = schema_collector(logs, store)
    (snap,) = snapshots(await collector.collect_scope("tbl-1", since=at(0), until=NOW))
    broken = snap.model_copy(
        update={"event_id": "evt-x", "dedupe_key": "x", "payload": {**snap.payload, "version": "7"}}
    )
    await store.append([broken])
    assert await collector._stored_baseline("tbl-1") is None  # a snapshot that does not say its version is skipped
    assert [e.event_type for e in await store.query(EventQuery(correlation_id="tbl-1"))] == [EventType.SCHEMA_SNAPSHOT]


# ------------------------------------------------------------------------------------------------ capacity
async def test_capacity_is_read_over_at_most_the_last_day() -> None:
    from orqentis.domain.estate import Capacity

    capacities = FakeCapacities()
    cat = catalog()
    cat.capacities["cap-1"] = Capacity(
        id="cap-1", tenant_id="t", display_name="F64", sku="F64", environment=Environment.PROD
    )
    collector = CapacityCollector(FakeFabric(capacities=capacities), cat)  # type: ignore[arg-type]
    events = await collector.collect_scope("cap-1", since=NOW - timedelta(days=7), until=NOW)
    assert capacities.windows == [("cap-1", NOW - timedelta(hours=24), NOW)]
    assert {e.event_type for e in events} == {EventType.CAPACITY_METRIC, EventType.CAPACITY_THROTTLING}
    await collector.collect_scope("cap-1", since=NOW - timedelta(minutes=20), until=NOW)
    assert capacities.windows[-1][1] == NOW - timedelta(minutes=20)  # a recent watermark is respected
