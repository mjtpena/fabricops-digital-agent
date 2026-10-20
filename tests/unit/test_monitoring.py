"""The monitoring module, the metrics it reads and the operations guide describe the same thing.

The KQL cannot be executed offline (the dev test fire does that, ``docs/operations.md`` section 7). What these tests pin
instead is everything around it: each alert has what an on-call needs, the queries read only metrics and attributes the
code emits, the runbook links land on real headings, and no receiver or webhook is ever committed.
"""

from __future__ import annotations

import re

import pytest

from orqentis.observability.metrics import Metrics
from tests.conftest import REPO
from tests.support.metrics import MetricsHarness

BICEP_DIR = REPO / "infra" / "bicep"
MODULE = (BICEP_DIR / "modules" / "monitoring.bicep").read_text(encoding="utf-8")
MAIN = (BICEP_DIR / "main.bicep").read_text(encoding="utf-8")
README = (BICEP_DIR / "README.md").read_text(encoding="utf-8")
OPERATIONS = (REPO / "docs" / "operations.md").read_text(encoding="utf-8")
METRICS_SOURCE = (REPO / "src" / "orqentis" / "observability" / "metrics.py").read_text(encoding="utf-8")

ALERT_TYPES = ("Microsoft.Insights/metricAlerts", "Microsoft.Insights/scheduledQueryRules")
EXPECTED_ALERTS = {
    "appDown": "alert-app-down",
    "restartLoop": "alert-restart-loop",
    "http5xx": "alert-5xx-rate",
    "collectorErrors": "alert-collector-errors",
    "heartbeat": "alert-collection-heartbeat",
    "auditVerification": "alert-audit-verification-failure",
}


def resource_blocks(text: str) -> dict[str, tuple[str, str]]:
    """``{symbolic name: (resource type, body)}`` for every top-level resource of a Bicep file."""
    starts = list(re.finditer(r"^resource (\w+) '([^'@]+)@[^']+' = \{", text, re.M))
    blocks: dict[str, tuple[str, str]] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        body = text[match.start() : end]
        blocks[match.group(1)] = (match.group(2), body.split("\n// ====", 1)[0])
    return blocks


def alert_rules() -> dict[str, str]:
    return {name: body for name, (kind, body) in resource_blocks(MODULE).items() if kind in ALERT_TYPES}


def module_params() -> dict[str, str | None]:
    """``{parameter: default expression or None}`` of the monitoring module."""
    return {
        name: (default.strip() if default else None)
        for name, default in re.findall(r"^param (\w+) \w+(?: = (.+))?$", MODULE, re.M)
    }


def github_anchors(markdown: str) -> set[str]:
    anchors = set()
    for heading in re.findall(r"^#{1,6} +(.+?) *$", markdown, re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower().replace("`", "")).replace(" ", "-")
        anchors.add(slug)
    return anchors


# ------------------------------------------------------------------------------------------------ every alert
def test_there_are_exactly_the_six_alerts_of_the_brief() -> None:
    assert set(alert_rules()) == set(EXPECTED_ALERTS)


@pytest.mark.parametrize("name", sorted(EXPECTED_ALERTS))
def test_every_alert_has_a_severity_a_window_a_threshold_the_action_group_and_a_runbook_link(name: str) -> None:
    body = alert_rules()[name]
    assert re.search(r"^\s+severity: [0-4]$", body, re.M), f"{name}: no severity"
    window = re.search(r"windowSize: 'PT\$\{(\w+)\}M'", body)
    assert window is not None, f"{name}: the window must come from a parameter"
    assert window.group(1) in module_params()
    threshold = re.search(r"^\s+threshold: (\S+)$", body, re.M)
    assert threshold is not None, f"{name}: no threshold"
    assert threshold.group(1) in module_params() or threshold.group(1).isdigit()
    assert "actionGroup.id" in body, f"{name}: does not use the action group"
    runbook_var = f"runbook{name[0].upper()}{name[1:]}"
    assert f"Runbook: ${{{runbook_var}}}" in body, f"{name}: the description must carry the runbook link"
    assert f"runbook: {runbook_var}" in body, f"{name}: the notification must carry the runbook link"


def test_the_tunable_thresholds_are_parameters_and_only_structural_ones_are_literals() -> None:
    rules = alert_rules()
    literal = {
        name: re.search(r"^\s+threshold: (\S+)$", body, re.M).group(1)  # type: ignore[union-attr]
        for name, body in rules.items()
    }
    # "no replica", "no successful run" and "any audit failure" are conditions, not tunables.
    assert {n: v for n, v in literal.items() if v.isdigit()} == {
        "appDown": "1",
        "heartbeat": "1",
        "auditVerification": "0",
    }
    assert literal["restartLoop"] == "restartThreshold"
    assert literal["http5xx"] == "http5xxRatePercent"
    assert literal["collectorErrors"] == "collectorErrorRuns"


def test_every_threshold_and_window_parameter_has_a_documented_default_and_reaches_main_and_the_readme() -> None:
    params = module_params()
    tunables = [
        "appDownWindowMinutes",
        "restartWindowMinutes",
        "restartThreshold",
        "http5xxWindowMinutes",
        "http5xxRatePercent",
        "http5xxMinRequests",
        "collectorErrorWindowMinutes",
        "collectorErrorRuns",
        "heartbeatMinutes",
        "auditFailureWindowMinutes",
        "evaluationFrequencyMinutes",
    ]
    for name in tunables:
        assert params[name] is not None and params[name].isdigit(), f"{name} needs a numeric default"  # type: ignore[union-attr]
        main_name = f"alert{name[0].upper()}{name[1:]}"
        assert re.search(rf"^param {main_name} int = {params[name]}$", MAIN, re.M), (
            f"{main_name} (default {params[name]})"
        )
        assert f"{name}: {main_name}" in MAIN, f"main.bicep does not pass {main_name} to the module"
        assert f"`{main_name}`" in README, f"{main_name} is not documented in infra/bicep/README.md"
    # Every parameter without a default is supplied by main.bicep.
    for name, default in params.items():
        if default is None:
            assert f"{name}: " in MAIN.split("module monitoring", 1)[1], f"main.bicep does not pass {name}"


def test_nothing_is_hard_coded_so_what_if_works_in_any_subscription() -> None:
    assert "/subscriptions/" not in MODULE and "subscription()" not in MODULE
    assert "resourceId(" not in MODULE
    for scope in re.findall(r"scopes: \[(\w+)\]", MODULE):
        assert scope in {"logAnalyticsWorkspaceId", "containerAppId"}
    urls = set(re.findall(r"https?://[^\s'\"]+", MODULE))
    assert urls == {
        # the runbook base default and the workbook schema
        "https://github.com/mjtpena/fabricops-digital-agent/blob/main/docs/operations.md",
        "https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json",
    }, "the module may reference no other fixed endpoint"


def test_the_runbook_links_land_on_real_headings_of_the_operations_guide() -> None:
    anchors = github_anchors(OPERATIONS)
    for name, anchor in EXPECTED_ALERTS.items():
        assert f"var runbook{name[0].upper()}{name[1:]} = '${{runbookBaseUrl}}#{anchor}'" in MODULE
        assert anchor in anchors, f"docs/operations.md has no heading for #{anchor}"
    assert "docs/operations.md" in re.search(r"param runbookBaseUrl string = '([^']+)'", MODULE).group(1)  # type: ignore[union-attr]


def test_the_heartbeat_is_switched_off_with_the_scheduler_and_the_collect_job() -> None:
    assert "heartbeatAlertEnabled: schedulerEnabled && collectEnabled" in MAIN
    assert re.search(r"severity: 1\s+enabled: heartbeatAlertEnabled", alert_rules()["heartbeat"])


def test_monitoring_is_added_to_main_additively_behind_a_switch() -> None:
    assert "module monitoring 'modules/monitoring.bicep' = if (monitoringEnabled) {" in MAIN
    assert "param monitoringEnabled bool = true" in MAIN
    assert "logAnalyticsWorkspaceId: logAnalytics.id" in MAIN and "containerAppId: containerApp.id" in MAIN


# ----------------------------------------------------------------------------------------------- no receivers
@pytest.mark.security
def test_the_webhook_is_secure_and_receivers_are_never_in_a_parameter_file() -> None:
    assert re.search(r"@secure\(\)\s+param teamsWebhookUri string = ''", MODULE)
    assert re.search(r"@secure\(\)\s+param alertTeamsWebhookUri string = ''", MAIN)
    allowed = (r"param teamsWebhookUri string = ''", r"empty\(teamsWebhookUri\)", r"serviceUri: teamsWebhookUri")
    for line in (line for line in MODULE.splitlines() if "teamsWebhookUri" in line):
        assert any(re.search(pattern, line) for pattern in allowed), (
            f"the secure value is used unexpectedly: {line.strip()}"
        )
    assert not re.search(r"^output .*teamsWebhookUri", MODULE, re.M | re.I)
    assert not re.search(r"^output .*alertTeamsWebhookUri", MAIN, re.M)
    files = sorted(BICEP_DIR.rglob("*.bicepparam"))
    assert files
    for path in files:
        body = path.read_text(encoding="utf-8")
        for line in body.splitlines():
            if line.lstrip().startswith("//"):
                continue
            if "alertTeamsWebhookUri" in line:
                assert "readEnvironmentVariable('ALERT_TEAMS_WEBHOOK_URI', '')" in line, f"{path.name}: {line}"
        assert not re.search(r"https?://\S*(webhook|logic\.azure|office\.com)", body, re.I), path.name
        emails = re.findall(r"[\w.+-]+@([\w-]+\.[\w.-]+)", body)
        assert all(domain in {"example.com", "example.org", "contoso.com"} for domain in emails), (path.name, emails)
        if "alertEmailReceivers" in body and path.parent.name == "env":
            assert "readEnvironmentVariable('ALERT_EMAIL_RECEIVERS', '')" in body, path.name


# ----------------------------------------------------------------------------- the queries and the code agree
# A quoted metric name; a prefix such as 'orqentis.scheduler.job.' (it ends in a dot) is not one.
METRIC_REFERENCE = re.compile(r"'(orqentis\.[a-z_.]*[a-z_])'")


def queries() -> list[str]:
    return re.findall(r"'''(.*?)'''", MODULE, re.S)


def instrument_names() -> set[str]:
    return set(re.findall(r'"(orqentis\.[a-z_.]+)"', METRICS_SOURCE))


def emitted_attributes() -> dict[str, set[str]]:
    """Attribute keys each instrument is really recorded with, by running every recording helper once."""
    h = MetricsHarness()
    m: Metrics = h.metrics
    m.collector_run("c", seconds=1, errors=1, new_events=1)
    m.collection_run("success", at=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC))
    m.incident_opened(category="X", severity="HIGH", environment="PROD", detection_latency_seconds=1)
    m.investigation_finished(outcome="succeeded", trigger="API", seconds=1)
    m.tool_call("t", "ERROR")
    m.llm_usage(model="m", input_tokens=1, output_tokens=1, cost_usd=1.0)
    m.reasoner_fallback("r")
    m.set_approvals_pending(1)
    m.job_finished("j", outcome="failed", seconds=1)
    m.job_overrun("j")
    m.lease_changed("lost")
    m.set_leader(True)
    m.notification("c", "sent")
    m.audit_verification("valid")
    return {name: h.attribute_keys(name) for name in h.names()}


def test_every_metric_the_queries_read_exists_and_every_instrument_is_in_the_operations_guide() -> None:
    read = {name for query in queries() for name in METRIC_REFERENCE.findall(query)}
    assert read, "no metric is read by any query"
    assert read <= instrument_names(), f"queries read metrics the code never emits: {sorted(read - instrument_names())}"
    emitted = set(emitted_attributes())
    assert instrument_names() == emitted, (
        f"instruments created but never recorded by a helper: {sorted(instrument_names() - emitted)}"
    )
    for name in instrument_names():
        assert f"`{name}`" in OPERATIONS or f"`{name}` (" in OPERATIONS, (
            f"{name} is not documented in docs/operations.md"
        )


def test_every_attribute_a_query_reads_is_one_the_code_records() -> None:
    emitted = emitted_attributes()
    for query in queries():
        names = set(METRIC_REFERENCE.findall(query))
        keys = set(re.findall(r"Properties\.(\w+)", query))
        if not names:
            continue
        available = set().union(*(emitted[n] for n in names))
        assert keys <= available, (
            f"{sorted(keys - available)} are not attributes of {sorted(names)} (they are {sorted(available)})"
        )


def test_the_log_lines_the_queries_look_for_are_the_ones_the_code_writes() -> None:
    sources = {
        "orqentis process started": "src/orqentis/runtime/service.py",
        "audit chain verification failed": "src/orqentis/runtime/audit_verification.py",
        "audit log integrity breach": "src/orqentis/observability/audit.py",
    }
    joined = "\n".join(queries())
    for phrase, source in sources.items():
        assert f"'{phrase}'" in joined, f"no query looks for {phrase!r}"
        assert phrase in (REPO / source).read_text(encoding="utf-8"), f"{source} no longer writes {phrase!r}"
