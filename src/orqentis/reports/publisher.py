"""Persist rendered reports next to the incident's evidence."""

from __future__ import annotations

from orqentis.domain.incidents import Incident
from orqentis.reports.builder import ReportBuilder
from orqentis.reports.markdown import render_markdown
from orqentis.reports.model import IncidentReport
from orqentis.storage.evidence_store import EvidenceStore


class ReportPublisher:
    def __init__(self, builder: ReportBuilder, store: EvidenceStore) -> None:
        self._builder = builder
        self._store = store

    def build(self, incident: Incident) -> IncidentReport:
        return self._builder.build(incident)

    async def publish(self, incident: Incident) -> tuple[IncidentReport, str]:
        report = self._builder.build(incident)
        await self._store.put_json(incident.incident_id, "report.json", report.model_dump(mode="json"), immutable=False)
        uri = await self._store.put_text(incident.incident_id, "report.md", render_markdown(report))
        return report, uri
