"""Markdown rendering of an :class:`IncidentReport`."""

from __future__ import annotations

from typing import Any

from orqentis.reports.model import IncidentReport, ReportStatement


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _statement(statement: ReportStatement, incident_id: str) -> str:
    refs = ""
    if statement.evidence_ids:
        short = [e.removeprefix(f"{incident_id}-") for e in statement.evidence_ids]
        refs = f" _(evidence: {', '.join(short)})_"
    return f"- **[{statement.kind.value}]** {statement.text}{refs}"


def render_markdown(report: IncidentReport) -> str:
    lines = [
        f"# Incident Report: {report.incident_id}",
        "",
        f"**{report.title}**",
        "",
        "| Incident | Severity | Current Status | Category | Environment | Confidence |",
        "|---|---|---|---|---|---|",
        f"| {report.incident_id} | {report.severity} | {report.status} | {report.category} | {report.environment} | "
        + (f"{report.confidence_level} ({report.confidence_score:.2f})" if report.confidence_score is not None else "-")
        + " |",
        "",
        f"Primary item: `{report.primary_item}` · Detected: {report.detected_at_local} · "
        f"Report generated: {report.generated_at_local} · Trace: `{report.agent_trace_id or '-'}` · "
        f"Reasoner: {report.reasoner or '-'}",
        "",
        "> Statements are labelled **FACT** (observed telemetry), **INFERENCE** (reasoned from cited evidence) "
        "or **RECOMMENDATION** (retrieved from a version-controlled runbook).",
    ]
    for section in report.sections:
        lines += ["", f"## {section.title}", ""]
        lines += [_statement(s, report.incident_id) for s in section.statements]
        if section.table:
            headers = list(section.table[0].keys())
            lines += ["", "| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
            lines += ["| " + " | ".join(_cell(row.get(h, "")) for h in headers) + " |" for row in section.table]
    return "\n".join(lines) + "\n"
