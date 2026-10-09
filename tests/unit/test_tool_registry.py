"""The tool boundary: typed responses, allowlists, validation, error mapping, budget, audit."""

from __future__ import annotations

from orqentis.bootstrap import Container
from orqentis.domain.principals import AGENT_PRINCIPAL, SYSTEM_PRINCIPAL
from orqentis.tools.models import ToolStatus
from orqentis.tools.registry import ToolContext


def ctx(container: Container, **kwargs: object) -> ToolContext:
    return ToolContext(principal=kwargs.pop("principal", SYSTEM_PRINCIPAL), trace_id="t-1", **kwargs)  # type: ignore[arg-type]


async def test_every_response_has_the_required_envelope(container: Container) -> None:
    response = await container.registry.invoke(
        "get_pipeline_run", {"pipeline_id": "pl-ingest-sales-prod", "run_id": "run-sales-20261005"}, ctx(container)
    )
    assert response.status is ToolStatus.SUCCESS
    for field in ("tool_call_id", "timestamp", "source", "status", "data", "errors", "metadata"):
        assert getattr(response, field) is not None
    assert response.source == "fabric.jobs"
    assert response.data["status"] == "Failed"
    assert response.metadata["latency_ms"] >= 0


async def test_tool_calls_are_logged_and_audited(container: Container) -> None:
    context = ctx(container)
    response = await container.registry.invoke("get_workspace", {"workspace_id": "ws-sales-prod"}, context)
    assert [c.tool_call_id for c in context.call_log] == [response.tool_call_id]
    records = [r for r in await container.audit.list() if r.action == "tool.invoke"]
    assert records[-1].details["tool_call_id"] == response.tool_call_id


async def test_allowlist_is_enforced(container: Container) -> None:
    context = ctx(container, principal=AGENT_PRINCIPAL, allowed_tools=frozenset({"get_workspace"}))
    response = await container.registry.invoke("get_item", {"item_id": "pl-ingest-sales-prod"}, context)
    assert response.status is ToolStatus.NOT_ALLOWED
    assert response.errors[0].code == "TOOL_NOT_ALLOWED"
    unknown = await container.registry.invoke("execute_action", {}, ctx(container))
    assert unknown.status is ToolStatus.NOT_ALLOWED and unknown.errors[0].code == "UNKNOWN_TOOL"


async def test_input_validation(container: Container) -> None:
    response = await container.registry.invoke("get_item", {"item_id": "../../etc/passwd"}, ctx(container))
    assert response.status is ToolStatus.ERROR
    assert response.errors[0].code == "INVALID_ARGUMENTS"
    extra = await container.registry.invoke("get_item", {"item_id": "x", "sql": "drop"}, ctx(container))
    assert extra.errors[0].code == "INVALID_ARGUMENTS"


async def test_not_found_is_mapped(container: Container) -> None:
    response = await container.registry.invoke("get_item", {"item_id": "does-not-exist"}, ctx(container))
    assert response.status is ToolStatus.NOT_FOUND
    run = await container.registry.invoke(
        "get_pipeline_run", {"pipeline_id": "pl-ingest-sales-prod", "run_id": "missing"}, ctx(container)
    )
    assert run.status is ToolStatus.NOT_FOUND and run.errors[0].code == "JobInstanceNotFound"


async def test_budget_is_enforced(container: Container) -> None:
    context = ctx(container, max_calls=1)
    first = await container.registry.invoke("get_workspace", {"workspace_id": "ws-sales-prod"}, context)
    second = await container.registry.invoke("get_workspace", {"workspace_id": "ws-sales-prod"}, context)
    assert first.status is ToolStatus.SUCCESS
    assert second.status is ToolStatus.NOT_ALLOWED and second.errors[0].code == "TOOL_BUDGET_EXHAUSTED"


async def test_secrets_are_redacted_in_tool_output(tmp_path: object) -> None:
    from pathlib import Path

    from orqentis.bootstrap import build_container
    from tests.conftest import make_settings

    c = await build_container(make_settings(Path(str(tmp_path)), scenario="auth_failure"), reset=True)
    try:
        response = await c.registry.invoke(
            "get_pipeline_run", {"pipeline_id": "pl-ingest-finance-prod", "run_id": "run-fin-20261005"}, ctx(c)
        )
        assert "Zx9~sQ.8hY2" not in str(response.data)
        assert "client_secret=[REDACTED]" in response.data["failure"]["message"]
    finally:
        await c.aclose()
