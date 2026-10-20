"""IncidentRepository contract."""

from __future__ import annotations

from tests.contract.conftest import history

from orqentis.domain.incidents import IncidentStatus
from orqentis.storage.incidents import IncidentQuery, IncidentRepository


async def test_save_get_round_trip_and_update(incident_repository: IncidentRepository) -> None:
    incident = history()[0]
    assert await incident_repository.get(incident.incident_id) is None
    await incident_repository.save(incident)
    assert await incident_repository.get(incident.incident_id) == incident
    changed = incident.model_copy(update={"status": IncidentStatus.INVESTIGATING})
    await incident_repository.save(changed)
    stored = await incident_repository.get(incident.incident_id)
    assert stored is not None and stored.status is IncidentStatus.INVESTIGATING
    assert len(await incident_repository.list()) == 1


async def test_list_filters_orders_newest_first_and_limits(incident_repository: IncidentRepository) -> None:
    incidents = history()
    assert len(incidents) >= 2
    for incident in incidents:
        await incident_repository.save(incident)
    listed = await incident_repository.list()
    assert [i.incident_id for i in listed] == [
        i.incident_id for i in sorted(incidents, key=lambda i: (i.detected_at, i.incident_id), reverse=True)
    ]
    first = incidents[0]
    by_item = await incident_repository.list(IncidentQuery(primary_item_id=first.primary_item_id))
    assert {i.incident_id for i in by_item} == {
        i.incident_id for i in incidents if i.primary_item_id == first.primary_item_id
    }
    assert [i.incident_id for i in await incident_repository.list(IncidentQuery(statuses=[first.status]))]
    assert all(
        i.category == first.category for i in await incident_repository.list(IncidentQuery(category=first.category))
    )
    assert len(await incident_repository.list(IncidentQuery(limit=1))) == 1
    assert await incident_repository.list(IncidentQuery(workspace_id="no-such-workspace")) == []


async def test_find_by_correlation_returns_the_newest_match(incident_repository: IncidentRepository) -> None:
    base = history()[0]
    older = base.model_copy(update={"incident_id": "INC-2026-0501", "correlation_id": "run-9"})
    later = older.detected_at.replace(year=older.detected_at.year + 1)
    newer = older.model_copy(update={"incident_id": "INC-2026-0502", "detected_at": later})
    await incident_repository.save(older)
    await incident_repository.save(newer)
    found = await incident_repository.find_by_correlation(base.primary_item_id, "run-9")
    assert found is not None and found.incident_id == "INC-2026-0502"
    assert await incident_repository.find_by_correlation(base.primary_item_id, "run-0") is None
    assert await incident_repository.find_by_correlation("other-item", "run-9") is None


async def test_incident_numbers_are_sequential_per_year_and_continue_after_stored_ones(
    incident_repository: IncidentRepository,
) -> None:
    base = history()[0]
    first = await incident_repository.next_incident_id(2031)
    assert first == "INC-2031-0001"
    await incident_repository.save(base.model_copy(update={"incident_id": first}))
    assert await incident_repository.next_incident_id(2031) == "INC-2031-0002"
    assert await incident_repository.next_incident_id(2032) == "INC-2032-0001"  # years are independent
    stored = base.model_copy(update={"incident_id": "INC-2033-0042"})
    await incident_repository.save(stored)  # loaded history, a migrated database: numbering continues after it
    assert await incident_repository.next_incident_id(2033) == "INC-2033-0043"
