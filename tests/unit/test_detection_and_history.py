"""Detection rules, de-duplication and incident history search."""

from __future__ import annotations

from datetime import timedelta

from orqentis.bootstrap import Container
from orqentis.config.clock import FixedClock
from orqentis.domain.events import EventType
from orqentis.domain.taxonomy import Severity
from orqentis.storage.event_store import EventQuery
from tests.conftest import detect


async def test_failed_pipeline_run_opens_one_incident(container: Container) -> None:
    incident = await detect(container)
    assert incident.incident_id == "INC-2026-0042"  # continues after the historical INC-2026-0041
    assert incident.title == "Pipeline ingest_sales_prod failed"
    assert incident.severity is Severity.HIGH
    assert incident.correlation_id == "run-sales-20261005"
    assert len(incident.symptoms) == 2  # run failure + activity failure attached
    created = await container.event_store.query(EventQuery(event_types=[EventType.INCIDENT_CREATED]))
    assert len(created) == 1


async def test_detection_is_idempotent(container: Container) -> None:
    await detect(container)
    _, created_again = await container.collect_and_detect()
    assert created_again == []
    # replaying the same events through detection does not create duplicates either
    events = await container.event_store.query(EventQuery(event_types=[EventType.PIPELINE_RUN_FAILED]))
    assert await container.detection.process(events) == []


async def test_old_failures_are_not_paged(container: Container) -> None:
    assert isinstance(container.clock, FixedClock)
    container.clock.advance(timedelta(days=2))
    _, created = await container.collect_and_detect()
    assert created == []


async def test_history_search_ranks_same_item_and_signature(container: Container) -> None:
    incident = await detect(container)
    history = container.investigations.workflow_for(incident)
    assert history is not None
    from orqentis.incidents.classification import ErrorClassifier
    from orqentis.incidents.history import IncidentHistory

    search = IncidentHistory(container.incidents.repository, ErrorClassifier(), timezone="Australia/Sydney")
    matches = await search.search_related(
        primary_item_id=incident.primary_item_id,
        before=incident.detected_at,
        exclude_incident_id=incident.incident_id,
        category_hint="SCHEMA_DRIFT",
        error_text='expected column "customer_id" is missing from input schema',
    )
    assert matches[0].incident_id == "INC-2026-0041"
    assert matches[0].similarity > 0.9
    assert "same error signature SIG-SCHEMA-MISSING-COLUMN" in matches[0].reasons
    assert all(m.incident_id != incident.incident_id for m in matches)
