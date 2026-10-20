"""Creates :class:`Evidence` on an incident and persists raw artefacts to the evidence store."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from orqentis.config.clock import Clock
from orqentis.domain.evidence import Evidence, EvidenceType
from orqentis.domain.incidents import Incident
from orqentis.observability.redaction import redact, redact_text
from orqentis.storage.evidence_store import EvidenceStore
from orqentis.tools.models import ToolResponse
from orqentis.tools.untrusted import scan

if TYPE_CHECKING:
    from orqentis.tools.registry import ToolOutcome


class EvidenceRecorder:
    def __init__(self, incident: Incident, store: EvidenceStore, clock: Clock) -> None:
        self._incident = incident
        self._store = store
        self._clock = clock
        # Agent tool calls can run concurrently; id allocation + persistence must be atomic.
        self._lock = asyncio.Lock()
        # Raw artefacts already in the store for this incident (read once, on the first record).
        self._stored: set[str] | None = None

    @property
    def incident(self) -> Incident:
        return self._incident

    async def _next_id(self) -> str:
        """Next free evidence id.

        An investigation that was interrupted (error, timeout, shutdown) leaves its immutable raw artefacts behind
        while the incident forgets the evidence that referenced them. Ids already used in the store are skipped, so a
        retry never collides with, or overwrites, what an earlier attempt recorded.
        """
        if self._stored is None:
            uris = await self._store.list(self._incident.incident_id)
            self._stored = {uri.rsplit("/", 1)[-1].removesuffix(".json") for uri in uris}
        taken = {e.evidence_id for e in self._incident.evidence} | self._stored
        n = len(self._incident.evidence) + 1
        while (candidate := f"{self._incident.incident_id}-EV{n:03d}") in taken:
            n += 1
        return candidate

    async def record(
        self,
        *,
        evidence_type: EvidenceType,
        summary: str,
        source: str,
        resource_id: str | None = None,
        resource_type: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
        confidence: float = 1.0,
        tool_response: ToolResponse | None = None,
        raw: Any = None,
    ) -> Evidence:
        async with self._lock:
            return await self._record(
                evidence_type=evidence_type,
                summary=summary,
                source=source,
                resource_id=resource_id,
                resource_type=resource_type,
                structured_payload=structured_payload,
                observed_at=observed_at,
                confidence=confidence,
                tool_response=tool_response,
                raw=raw,
            )

    async def _record(
        self,
        *,
        evidence_type: EvidenceType,
        summary: str,
        source: str,
        resource_id: str | None,
        resource_type: str | None,
        structured_payload: dict[str, Any] | None,
        observed_at: datetime | None,
        confidence: float,
        tool_response: ToolResponse | None,
        raw: Any,
    ) -> Evidence:
        evidence_id = await self._next_id()
        raw_payload = raw if raw is not None else (tool_response.model_dump(mode="json") if tool_response else None)
        raw_reference = (
            await self._store.put_json(self._incident.incident_id, f"{evidence_id}.json", raw_payload)
            if raw_payload is not None
            else None
        )
        payload = redact(structured_payload or {})
        clean_summary = redact_text(summary)
        evidence = Evidence(
            evidence_id=evidence_id,
            incident_id=self._incident.incident_id,
            source=source,
            resource_id=resource_id,
            resource_type=resource_type,
            collected_at=self._clock.now(),
            observed_at=observed_at,
            evidence_type=evidence_type,
            summary=clean_summary,
            raw_reference=raw_reference,
            structured_payload=payload,
            confidence=confidence,
            tool_call_id=tool_response.tool_call_id if tool_response else None,
            untrusted_flags=scan([clean_summary, payload]),
        )
        self._incident.evidence.append(evidence)
        return evidence

    async def record_tool_response(
        self, response: ToolResponse, evidence_type: EvidenceType, outcome: ToolOutcome
    ) -> Evidence:
        data = response.data
        payload = data if isinstance(data, dict) else {"items": data}
        return await self.record(
            evidence_type=evidence_type,
            summary=outcome.summary,
            source=response.source,
            resource_id=outcome.resource_id,
            resource_type=outcome.resource_type,
            structured_payload=payload,
            observed_at=outcome.observed_at,
            tool_response=response,
        )
