"""Local storage adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orqentis.domain.events import EventType, OperationalEvent
from orqentis.storage.event_store import EventQuery, LocalEventStore
from orqentis.storage.evidence_store import EvidenceExistsError, LocalEvidenceStore
from orqentis.storage.sqlite import SqliteDatabase

T0 = datetime(2026, 10, 5, tzinfo=UTC)


def event(i: int, item: str = "a", kind: EventType = EventType.PIPELINE_RUN_SUCCEEDED) -> OperationalEvent:
    return OperationalEvent(
        event_id=f"e{i}",
        event_type=kind,
        tenant_id="t",
        item_id=item,
        timestamp=T0 + timedelta(minutes=i),
        summary="s",
        dedupe_key=f"k{i}",
        source="test",
    )


async def test_event_store_dedupes_and_queries(tmp_path: Path) -> None:
    store = LocalEventStore(SqliteDatabase(tmp_path / "e.sqlite3"))
    assert len(await store.append([event(1), event(2, "b", EventType.SCHEMA_CHANGED), event(3)])) == 3
    assert await store.append([event(1)]) == []
    assert await store.count() == 3
    assert [e.event_id for e in await store.query(EventQuery(item_ids=["a"]))] == ["e1", "e3"]
    assert [e.event_id for e in await store.query(EventQuery(event_types=[EventType.SCHEMA_CHANGED]))] == ["e2"]
    window = EventQuery(since=T0 + timedelta(minutes=2), until=T0 + timedelta(minutes=3), newest_first=True)
    assert [e.event_id for e in await store.query(window)] == ["e3", "e2"]
    assert await store.query(EventQuery(item_ids=[])) == []
    assert (await store.get("e2")) is not None


async def test_evidence_store_is_immutable_and_rejects_traversal(tmp_path: Path) -> None:
    store = LocalEvidenceStore(tmp_path / "ev")
    uri = await store.put_json("INC-1", "EV001.json", {"token": "abc", "v": 1})
    assert uri == "evidence://INC-1/EV001.json"
    assert (await store.get_json(uri)) == {"token": "[REDACTED]", "v": 1}
    with pytest.raises(EvidenceExistsError):
        await store.put_json("INC-1", "EV001.json", {"v": 2})
    for bad in ("../x", "a/b", ".hidden"):
        with pytest.raises(ValueError, match="Unsafe evidence path segment"):
            await store.put_text("INC-1", bad, "x")
    assert await store.list("INC-1") == [uri]
