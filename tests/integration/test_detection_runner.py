"""Durable detection: the cursor moves only after detection returned, and reprocessing is harmless."""

from __future__ import annotations

from typing import Any

import pytest

from orqentis.bootstrap import Container
from orqentis.config.clock import FixedClock
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.incidents.detection_runner import DetectionRunner


async def stored_but_undetected(container: Container) -> None:
    """Collect without detecting, as an interrupted collection run leaves things."""
    await container.ingestor.run()


async def newest_sequence(container: Container) -> int:
    return (await container.event_store.read_after(0, limit=10_000))[-1].sequence


async def open_incident_ids(container: Container) -> list[str]:
    return sorted(i.incident_id for i in await container.incidents.repository.list() if i.status.is_open)


async def test_the_cursor_advances_only_after_detection_returned_so_a_failure_is_retried(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    await stored_but_undetected(container)
    runner = DetectionRunner(container.event_store, container.detection, consumer="test")
    original = container.detection.process

    async def explode(events: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(container.detection, "process", explode)
    with pytest.raises(RuntimeError, match="locked"):
        await runner.run()
    assert await container.event_store.get_cursor("test") == 0  # nothing was marked as done
    assert await open_incident_ids(container) == []

    monkeypatch.setattr(container.detection, "process", original)
    created = await runner.run()
    assert [i.incident_id for i in created] == ["INC-2026-0042"]
    # Opening the incident stored an INCIDENT_CREATED event too; the runner has consumed that as well.
    assert await container.event_store.get_cursor("test") == await newest_sequence(container)
    assert await runner.run() == []  # caught up: nothing is processed twice


async def test_a_second_consumer_reading_the_same_events_does_not_open_duplicate_incidents(
    container: Container,
) -> None:
    await stored_but_undetected(container)
    first = await DetectionRunner(container.event_store, container.detection, consumer="one").run()
    second = await DetectionRunner(container.event_store, container.detection, consumer="two").run()
    assert [i.incident_id for i in first] == ["INC-2026-0042"] and second == []
    assert await open_incident_ids(container) == ["INC-2026-0042"]


async def test_reprocessing_an_event_that_has_no_correlation_id_opens_no_second_incident(
    container: Container,
) -> None:
    clock = container.clock
    assert isinstance(clock, FixedClock)
    event = OperationalEvent(
        event_id="evt-no-correlation",
        event_type=EventType.PIPELINE_RUN_FAILED,
        tenant_id="tenant-contoso",
        workspace_id="ws-sales-prod",
        item_id="pl-ingest-sales-prod",
        item_type=ItemType.DATA_PIPELINE,
        display_name="ingest_sales_prod",
        environment=Environment.PROD,
        timestamp=clock.now(),
        summary="Pipeline failed",
        correlation_id=None,  # nothing to de-duplicate on except the event itself
        dedupe_key="no-correlation",
        source="test",
    )
    first = await container.detection.process([event])
    again = await container.detection.process([event])  # what a re-read after an interruption does
    assert len(first) == 1 and again == []
    assert first[0].metadata["trigger_event_id"] == "evt-no-correlation"


async def test_large_backlogs_are_read_in_batches(container: Container) -> None:
    await stored_but_undetected(container)
    runner = DetectionRunner(container.event_store, container.detection, consumer="small-batches", batch_size=3)
    created = await runner.run()
    assert [i.incident_id for i in created] == ["INC-2026-0042"]
    assert await container.event_store.get_cursor("small-batches") == await newest_sequence(container)
    with pytest.raises(ValueError, match="batch_size"):
        DetectionRunner(container.event_store, container.detection, batch_size=0)
