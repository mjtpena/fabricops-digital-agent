"""The monitoring module, the metrics it reads and the operations guide describe the same thing.

The KQL cannot be executed offline (the dev test fire does that, ``docs/operations.md`` section 7). What these tests pin
instead is everything around it: each alert has what an on-call needs, the queries read only metrics and attributes the
code emits, the runbook links land on real headings, and no receiver or webhook is ever committed.
"""

from __future__ import annotations

import ast
import json
import logging
import re

import pytest

from orqentis.observability.logging import JsonFormatter
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
    window = re.search(r"windowSize: isoDuration\['\$\{(\w+)\}'\]", body)
    assert window is not None, f"{name}: the window must come from a parameter, through the ISO duration map"
    assert window.group(1) in module_params() or window.group(1) == "auditWindowMinutes"  # the derived one
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
# `tostring(log.logger) == 'orqentis.runtime.service'` names a logger, not a metric.
LOGGER_FIELD = re.compile(r"tostring\(log\.logger\) == '[^']+'")


def metric_names(query: str) -> set[str]:
    return set(METRIC_REFERENCE.findall(LOGGER_FIELD.sub("", query)))


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
    read = {name for query in queries() for name in metric_names(query)}
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
        names = metric_names(query)
        keys = set(re.findall(r"Properties\.(\w+)", query))
        if not names:
            continue
        available = set().union(*(emitted[n] for n in names))
        assert keys <= available, (
            f"{sorted(keys - available)} are not attributes of {sorted(names)} (they are {sorted(available)})"
        )


# ------------------------------------------------------------- log lines: exact fields, never a phrase anywhere
LOGGER_AND_MESSAGE = re.compile(r"tostring\(log\.logger\) == '([^']+)' and tostring\(log\.message\) == '([^']+)'")
HAS_PREFILTER = re.compile(r"Log_s has '([^']+)'")
# Phrases our own code writes. They must never be matched as a phrase: Fabric and the model put text in other lines.
OUR_PHRASES = ("orqentis process started", "audit chain verification failed", "audit log integrity breach")


def exact_pairs(query: str) -> set[tuple[str, str]]:
    return set(LOGGER_AND_MESSAGE.findall(query))


def application_log_queries() -> list[str]:
    return [q for q in queries() if "ContainerAppConsoleLogs_CL" in q]


def kql_matches(line: str, query: str) -> bool:
    """What the query's console-log leg does to one line: the ``has`` narrowing, then the exact JSON fields."""
    if not any(term in line for term in HAS_PREFILTER.findall(query)):
        return False
    try:
        log = json.loads(line)
    except ValueError:
        return False  # parse_json of a line that is not JSON leaves logger and message empty
    return isinstance(log, dict) and (str(log.get("logger")), str(log.get("message"))) in exact_pairs(query)


def log_line(logger: str, message: str, level: int = logging.INFO, **extra: object) -> str:
    record = logging.LogRecord(logger, level, __file__, 1, message, (), None)
    record.__dict__.update(extra)
    return JsonFormatter().format(record)


def console_legs(query: str) -> list[str]:
    """The part of a query that reads the application's console log (the platform's system log is not ours to doubt)."""
    return [
        m.group(1) for m in re.finditer(r"ContainerAppConsoleLogs_CL(.*?)(?:\| count\)|\| summarize|\Z)", query, re.S)
    ]


def test_console_log_alerts_match_the_json_fields_logger_and_message_not_a_phrase() -> None:
    queries_with_logs = application_log_queries()
    assert len(queries_with_logs) == 3  # the restart loop alert, the audit alert, the workbook's restart chart
    for query in queries_with_logs:
        assert "parse_json(Log_s)" in query, "the line must be parsed before it is judged"
        assert exact_pairs(query), "no exact logger + message match"
        # A phrase of ours may narrow the rows before the parse (`has`), but it is never the whole condition.
        for leg in console_legs(query):
            assert "parse_json(Log_s)" in leg
            assert "Log_s has" not in leg.split("parse_json(Log_s)", 1)[1], (
                "text matching after the parse would reopen the phrase match"
            )


def test_every_logger_and_message_the_queries_match_is_one_the_code_writes() -> None:
    pairs = set().union(*(exact_pairs(q) for q in queries()))
    assert pairs == {
        ("orqentis.runtime.service", "orqentis process started"),
        ("orqentis.runtime.audit_verification", "audit chain verification failed"),
        ("orqentis.observability.audit", "audit log integrity breach; appends are refused"),
    }
    for logger_name, message in pairs:
        source = REPO / "src" / (logger_name.replace(".", "/") + ".py")
        assert source.exists(), f"{logger_name} is not a module of the application"
        text = source.read_text(encoding="utf-8")
        assert f'"{message}"' in text, f"{source.name} no longer logs {message!r}"
        assert "get_logger(__name__)" in text, f"{source.name} must name its logger after the module"


def test_text_from_fabric_or_the_model_cannot_match_the_console_log_alerts() -> None:
    restart = next(q for q in application_log_queries() if "process started" in q and "audit" not in q)
    audit = next(q for q in application_log_queries() if "audit chain" in q)
    genuine_start = log_line("orqentis.runtime.service", "orqentis process started", version="0.1.0")
    genuine_failure = log_line(
        "orqentis.runtime.audit_verification", "audit chain verification failed", logging.ERROR, check="chain"
    )
    genuine_breach = log_line(
        "orqentis.observability.audit", "audit log integrity breach; appends are refused", logging.ERROR
    )
    assert (
        kql_matches(genuine_start, restart)
        and kql_matches(genuine_failure, audit)
        and kql_matches(genuine_breach, audit)
    )

    forged = json.dumps({"logger": "orqentis.runtime.audit_verification", "message": "audit chain verification failed"})
    hostile = [
        # a Fabric error that quotes our phrases, logged by the collector (the real `collector failed` call)
        log_line(
            "orqentis.telemetry.collectors",
            "collector failed",
            logging.WARNING,
            collector="pipeline-runs",
            error={"message": "audit chain verification failed; orqentis process started; audit log integrity breach"},
        ),
        # the model's exception text (the real `reasoner failed; using fallback` call)
        log_line(
            "orqentis.agents.investigator",
            "reasoner failed; using fallback",
            logging.WARNING,
            error="orqentis process started",
        ),
        # a whole forged log object and a forged second line, smuggled in as text
        log_line("orqentis.telemetry.collectors", "collector failed", logging.WARNING, error=forged),
        log_line("orqentis.telemetry.collectors", "collector failed", logging.WARNING, error=f"x\n{forged}\n{forged}"),
        # a forged line from the right logger name but the wrong message, and the right message from the wrong logger
        log_line("orqentis.runtime.audit_verification", "audit chain verified"),
        log_line("orqentis.telemetry.collectors", "audit chain verification failed"),
        # plain text, a traceback line, and a record whose exception text holds the phrase
        "audit chain verification failed",
        "Traceback: orqentis process started",
        log_line("orqentis.x", "boom", logging.ERROR, exception="Traceback ... audit chain verification failed"),
    ]
    # The test has teeth: a plain phrase search, which is what the alerts used to do, would have matched these.
    assert all(any(phrase in line for phrase in OUR_PHRASES) for line in hostile[:4])
    for line in hostile:
        assert "\n" not in line, "one log record is one physical line, whatever text it carries"
        assert not kql_matches(line, restart), line
        assert not kql_matches(line, audit), line


def test_no_alert_or_workbook_query_decides_on_a_phrase_of_ours_alone() -> None:
    for query in queries():
        if "ContainerAppConsoleLogs_CL" not in query:
            continue
        for phrase in OUR_PHRASES:
            for line in query.splitlines():
                if (
                    phrase in line
                    and not line.lstrip().startswith(("//", "| where Log_s has"))
                    and "tostring(log.message) ==" not in line
                ):
                    raise AssertionError(
                        f"{phrase!r} is used outside the narrowing and the exact field match: {line.strip()}"
                    )


def log_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"debug", "info", "warning", "error", "exception", "critical"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"_log", "log", "logger", "_logger"}
    ]


def test_every_log_message_in_the_application_is_a_constant_so_the_message_field_is_never_untrusted() -> None:
    """The alerts trust ``message``. Untrusted text goes in the extra fields, never in the message itself."""
    checked = 0
    for path in sorted((REPO / "src" / "orqentis").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for call in log_calls(tree):
            checked += 1
            first = call.args[0] if call.args else None
            constant = (isinstance(first, ast.Constant) and isinstance(first.value, str)) or (
                isinstance(first, ast.Name) and first.id in constants
            )
            assert constant, f"{path.relative_to(REPO)}:{call.lineno} logs a message that is not a string constant"
    assert checked > 50


# ------------------------------------------------- windows: only what Azure accepts, never shorter than the frequency
AZURE_LOG_WINDOWS = {
    5: "PT5M",
    10: "PT10M",
    15: "PT15M",
    30: "PT30M",
    45: "PT45M",
    60: "PT1H",
    120: "PT2H",
    180: "PT3H",
    240: "PT4H",
    300: "PT5H",
    360: "PT6H",
    1440: "P1D",
    2880: "P2D",
}
AZURE_METRIC_WINDOWS = {1, 5, 15, 30, 60}
AZURE_FREQUENCIES = {1, 5, 10, 15, 30, 45, 60}
WINDOW_PARAMS = {
    "restartWindowMinutes": 15,
    "http5xxWindowMinutes": 10,
    "collectorErrorWindowMinutes": 15,
    "heartbeatMinutes": 15,
}


def allowed_values(text: str, param: str) -> list[int]:
    match = re.search(rf"@allowed\(\[\s*([\d\s]+?)\s*\]\)\s+param {param} int", text)
    assert match is not None, f"{param} must be limited with @allowed([...])"
    return [int(v) for v in match.group(1).split()]


def iso_durations() -> dict[int, str]:
    body = re.search(r"var isoDuration = \{(.*?)\n\}", MODULE, re.S)
    assert body is not None
    return {int(k): v for k, v in re.findall(r"'(\d+)': '([A-Z0-9]+)'", body.group(1))}


def main_name(module_param: str) -> str:
    return f"alert{module_param[0].upper()}{module_param[1:]}"


@pytest.mark.parametrize("param", sorted(WINDOW_PARAMS))
def test_every_log_alert_window_is_limited_to_the_durations_azure_accepts_in_the_module_and_in_main(param: str) -> None:
    for text, name in ((MODULE, param), (MAIN, main_name(param))):
        assert set(allowed_values(text, name)) == set(AZURE_LOG_WINDOWS), name
        assert not re.search(rf"@(min|max)Value\([^)]*\)\s+(@\w+\([^)]*\)\s+)*param {name} ", text), (
            f"{name}: a range accepts values (20, 65) that Azure refuses"
        )
    default = int(module_params()[param])  # type: ignore[arg-type]
    assert default == WINDOW_PARAMS[param] and default in AZURE_LOG_WINDOWS


def test_the_audit_window_the_frequency_and_the_app_down_window_are_limited_too() -> None:
    for text, audit, frequency in (
        (MODULE, "auditFailureWindowMinutes", "evaluationFrequencyMinutes"),
        (MAIN, "alertAuditFailureWindowMinutes", "alertEvaluationFrequencyMinutes"),
    ):
        assert set(allowed_values(text, audit)) == {0, *AZURE_LOG_WINDOWS}
        assert set(allowed_values(text, frequency)) == AZURE_FREQUENCIES
    assert set(allowed_values(MODULE, "appDownWindowMinutes")) == AZURE_METRIC_WINDOWS
    assert set(allowed_values(MAIN, "alertAppDownWindowMinutes")) == AZURE_METRIC_WINDOWS
    assert module_params()["auditFailureWindowMinutes"] == "0"  # 0: derived from the verification interval


def test_minutes_are_mapped_to_exactly_the_iso_durations_azure_accepts() -> None:
    mapping = iso_durations()
    assert {m: d for m, d in mapping.items() if m in AZURE_LOG_WINDOWS} == AZURE_LOG_WINDOWS
    assert mapping[1] == "PT1M"  # a one-minute evaluation frequency
    assert {mapping[m] for m in AZURE_METRIC_WINDOWS} == {"PT1M", "PT5M", "PT15M", "PT30M", "PT1H"}
    assert set(mapping) >= AZURE_FREQUENCIES | AZURE_METRIC_WINDOWS | set(AZURE_LOG_WINDOWS)
    # No duration is ever formatted from minutes ('PT65M', 'PT60M' are refused by Azure).
    assert not re.search(r"'PT\$\{\w+\}M'", MODULE)
    assert re.search(r"var evaluationFrequency = isoDuration\['\$\{evaluationFrequencyMinutes\}'\]", MODULE)
    assert re.search(r"var logAlertWindowMinutes = \[([\d, ]+)\]", MODULE).group(1).replace(" ", "") == ",".join(  # type: ignore[union-attr]
        str(m) for m in sorted(AZURE_LOG_WINDOWS)
    )


def derive_audit_window(interval: int, explicit: int = 0) -> int:
    """The module's rule, in Python: at least twice the interval, rounded up to a window Azure offers."""
    wanted = explicit if explicit > 0 else 2 * interval
    candidates = [m for m in sorted(AZURE_LOG_WINDOWS) if m >= wanted]
    return candidates[0] if candidates else 2880


def test_the_audit_window_is_derived_from_the_verification_interval_and_cannot_flap() -> None:
    wanted = "auditFailureWindowMinutes > 0 ? auditFailureWindowMinutes : 2 * auditVerifyIntervalMinutes"
    assert f"var auditWindowWanted = {wanted}" in MODULE
    assert (
        "var auditWindowCandidates = filter(logAlertWindowMinutes, minutes => minutes >= auditWindowWanted)" in MODULE
    )
    assert "var auditWindowMinutes = empty(auditWindowCandidates) ? 2880 : auditWindowCandidates[0]" in MODULE
    assert "auditVerifyIntervalMinutes: auditVerifyIntervalMinutes" in MAIN
    assert derive_audit_window(60) == 120  # the default hourly verification
    for interval in (1, 5, 15, 30, 45, 60, 90, 180, 720, 1440):
        window = derive_audit_window(interval)
        assert window in AZURE_LOG_WINDOWS and window >= min(2 * interval, 2880), interval
    assert derive_audit_window(10080) == 2880  # longer than any window: the alert is sticky for this reason (below)
    assert derive_audit_window(60, explicit=360) == 360


def test_the_defaults_are_consistent_with_each_other() -> None:
    frequency = int(module_params()["evaluationFrequencyMinutes"])  # type: ignore[arg-type]
    assert frequency == 5
    assert all(default >= frequency for default in WINDOW_PARAMS.values())
    assert derive_audit_window(60) >= frequency


def test_a_window_shorter_than_the_evaluation_frequency_stops_the_deployment_with_a_readable_reason() -> None:
    assert "module alertWindowCheck 'input-check.bicep' = {" in MODULE
    assert "result: any(alertWindowProblem)" in MODULE
    assert (
        "windowsTooShort = filter(items(alertWindows), window => window.value < evaluationFrequencyMinutes)" in MODULE
    )
    checked = set(
        re.findall(r"^  (\w+): \w+$", re.search(r"var alertWindows = \{(.*?)\n\}", MODULE, re.S).group(1), re.M)
    )  # type: ignore[union-attr]
    assert checked == {main_name(p) for p in WINDOW_PARAMS} | {"alertAuditFailureWindowMinutes"}
    for name, body in alert_rules().items():
        if "scheduledQueryRules" in resource_blocks(MODULE)[name][0]:
            assert "dependsOn: [alertWindowCheck]" in body, f"{name} must wait for the check"


def test_the_cost_advice_for_a_slower_evaluation_also_raises_the_windows_that_would_be_too_short() -> None:
    cost = (REPO / "docs" / "cost.md").read_text(encoding="utf-8")
    advice = "alertEvaluationFrequencyMinutes=15"
    assert advice in cost
    too_short = [p for p, default in WINDOW_PARAMS.items() if default < 15]
    assert too_short == ["http5xxWindowMinutes"]
    assert "alertHttp5xxWindowMinutes=15" in cost, "a 15-minute frequency refuses the 10-minute 5xx window"


# --------------------------------------------------------------------------------- the audit alert is sticky
def test_the_audit_alert_does_not_resolve_itself_and_the_others_do() -> None:
    rules = alert_rules()
    audit = rules["auditVerification"]
    assert "autoMitigate: false" in audit and "muteActionsDuration: 'PT6H'" in audit
    for name, body in rules.items():
        if name != "auditVerification":
            assert "autoMitigate: true" in body, name


def test_emails_are_trimmed_and_empty_entries_dropped_in_every_environment_file() -> None:
    for env in ("dev", "test", "prod"):
        body = (BICEP_DIR / "env" / f"{env}.bicepparam").read_text(encoding="utf-8")
        assert (
            "filter(map(split(alertEmailList, ','), address => trim(address)), address => !empty(address))" in body
        ), env


def test_the_app_down_gap_is_documented_with_the_heartbeat_as_the_backstop() -> None:
    section = OPERATIONS.split("### Alert: app down", 1)[1].split("### Alert: restart loop", 1)[0]
    assert "Replicas" in section and "backstop" in section and "heartbeat" in section
    adr = (REPO / "docs" / "adr" / "0019-metrics-alerts-and-operations.md").read_text(encoding="utf-8")
    assert "backstop" in adr
