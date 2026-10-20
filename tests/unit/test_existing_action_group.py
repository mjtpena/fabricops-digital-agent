"""Alerts can notify a separately managed action group, so receivers never pass through the pipeline.

A GitHub Actions job prints its environment in its log, and the repository is public: a variable holding email addresses
would publish them. The pipeline therefore carries only the *identifier* of an action group created outside it
(``ALERT_ACTION_GROUP_ID``). When it is set the template creates no action group and every alert uses that one.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from tests.conftest import REPO

BICEP = REPO / "infra" / "bicep"
MODULE = (BICEP / "modules" / "monitoring.bicep").read_text(encoding="utf-8")
MAIN = (BICEP / "main.bicep").read_text(encoding="utf-8")
WORKFLOWS = REPO / ".github" / "workflows"
EXTERNAL = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-x"
    "/providers/Microsoft.Insights/actionGroups/ag-managed"
)


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_action_group_snapshots", REPO / "scripts" / "check_action_group_snapshots.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


# --------------------------------------------------------------------------------------------- parameter wiring
def test_the_module_and_main_take_an_optional_existing_action_group_id_that_defaults_to_the_old_behaviour() -> None:
    assert re.search(r"^param existingActionGroupId string = ''$", MODULE, re.M)
    assert re.search(r"^param alertExistingActionGroupId string = ''$", MAIN, re.M)
    assert "existingActionGroupId: alertExistingActionGroupId" in MAIN


@pytest.mark.parametrize("environment", ["dev", "test", "prod"])
def test_every_environment_reads_the_action_group_id_from_the_deployers_environment(environment: str) -> None:
    body = (BICEP / "env" / f"{environment}.bicepparam").read_text(encoding="utf-8")
    assert "param alertExistingActionGroupId = readEnvironmentVariable('ALERT_ACTION_GROUP_ID', '')" in body


def test_the_deploy_job_passes_the_identifier_and_never_a_receiver() -> None:
    path = WORKFLOWS / "deploy-environment.yml"
    job = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["deploy"]
    assert job["env"]["ALERT_ACTION_GROUP_ID"] == "${{ vars.ALERT_ACTION_GROUP_ID }}"
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        code = "\n".join(
            line for line in workflow.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
        )
        for receiver in ("ALERT_EMAIL_RECEIVERS", "ALERT_TEAMS_WEBHOOK_URI"):
            assert receiver not in code, f"{workflow.name} must never carry {receiver}: a public job log prints it"
        assert "secrets.ALERT" not in code


def test_the_snapshot_script_is_stable_whatever_the_developers_shell_holds() -> None:
    text = (REPO / "scripts" / "bicep-snapshot.sh").read_text(encoding="utf-8")
    assert "unset FABRIC_MODE ALERT_EMAIL_RECEIVERS ALERT_TEAMS_WEBHOOK_URI ALERT_ACTION_GROUP_ID" in text
    assert 'export ALERT_ACTION_GROUP_ID="$SNAPSHOT_ALERT_ACTION_GROUP_ID"' in text  # the one explicit opt-in


def test_ci_runs_the_offline_check_on_every_push() -> None:
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    runs = [step.get("run", "") for step in ci["jobs"]["bicep"]["steps"]]
    assert any("scripts/check-alert-action-group.sh" in run for run in runs)
    assert (REPO / "scripts" / "check-alert-action-group.sh").exists()


# ------------------------------------------------------------------------------- the template honours the id
def test_no_action_group_is_created_when_an_existing_one_is_given() -> None:
    assert "var useExistingActionGroup = !empty(existingActionGroupId)" in MODULE
    assert re.search(
        r"^resource actionGroup 'Microsoft\.Insights/actionGroups@[^']+' = if \(!useExistingActionGroup\) \{$",
        MODULE,
        re.M,
    )
    assert "output actionGroupCreatedByTemplate bool = !useExistingActionGroup" in MODULE


def test_every_alert_notifies_the_external_group_when_it_is_set_and_otherwise_the_templates_own() -> None:
    assert "var alertActionGroupId = useExistingActionGroup ? existingActionGroupId : actionGroup.id" in MODULE
    assert "output actionGroupId string = alertActionGroupId" in MODULE
    assert "actionGroup.id" not in MODULE.replace(
        "var alertActionGroupId = useExistingActionGroup ? existingActionGroupId : actionGroup.id", ""
    )
    uses = re.findall(r"actionGroups: \[(\w+)\]|actionGroupId: (\w+)", MODULE)
    assert len(uses) == 6, "six alerts"
    assert {a or b for a, b in uses} == {"alertActionGroupId"}


def test_malformed_ids_and_receivers_next_to_an_external_group_stop_the_deployment() -> None:
    assert "var alertInputProblem = alertWindowProblem != 'ok' ? alertWindowProblem : existingGroupProblem" in MODULE
    assert "must be the resource id of an action group" in MODULE
    assert "the receivers belong to that action group" in MODULE
    assert "(!empty(emailReceivers) || !empty(teamsWebhookUri))" in MODULE
    # The id's shape is checked without reading an index that might not exist (conditions are not short-circuited).
    assert "length(split(existingGroupLowercase, '/')) == 9" in MODULE
    assert not re.search(r"split\(existingGroup\w*, '/'\)\[", MODULE)


# --------------------------------------------------------------------------------------- the snapshot checker
def snapshot(*, group: str | None, alerts_use: list[str], alerts: int = 6) -> dict[str, Any]:
    resources: list[dict[str, Any]] = []
    if group is not None:
        resources.append({"type": checker.GROUP_TYPE, "name": group, "properties": {}})
    for index in range(alerts):
        if index == 0:  # a metric alert
            actions: Any = [{"actionGroupId": g} for g in alerts_use]
            resources.append({"type": checker.METRIC_ALERT, "name": f"m{index}", "properties": {"actions": actions}})
        else:
            resources.append(
                {
                    "type": checker.LOG_ALERT,
                    "name": f"q{index}",
                    "properties": {"actions": {"actionGroups": alerts_use}},
                }
            )
    return {"predictedResources": resources}


OWN = "/subscriptions/0/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/ag-fabricops-dev"


def good_default() -> dict[str, Any]:
    return snapshot(group="ag-fabricops-dev", alerts_use=[OWN])


def good_external() -> dict[str, Any]:
    return snapshot(group=None, alerts_use=[EXTERNAL])


def test_the_checker_accepts_the_two_correct_shapes() -> None:
    assert checker.problems_for("dev", good_default(), good_external(), EXTERNAL) == []
    # Resource ids are case-insensitive in Azure.
    shouting = snapshot(group=None, alerts_use=[EXTERNAL.upper()])
    assert checker.problems_for("dev", good_default(), shouting, EXTERNAL) == []


@pytest.mark.parametrize(
    ("default", "external", "expected"),
    [
        # the template still creates a group although an external one was given
        (good_default(), snapshot(group="ag-fabricops-dev", alerts_use=[EXTERNAL]), "still created"),
        # an alert ignores the external group
        (good_default(), snapshot(group=None, alerts_use=[OWN]), "does not use the external action group"),
        # an alert notifies both
        (good_default(), snapshot(group=None, alerts_use=[EXTERNAL, OWN]), "does not use the external action group"),
        # an alert is missing
        (good_default(), snapshot(group=None, alerts_use=[EXTERNAL], alerts=5), "expected 6 alert rules"),
        # by default there is no group, or an alert uses some other group
        (snapshot(group=None, alerts_use=[OWN]), good_external(), "exactly one action group"),
        (snapshot(group="ag-fabricops-dev", alerts_use=[EXTERNAL]), good_external(), "template's own action group"),
    ],
)
def test_the_checker_names_what_is_wrong(default: dict[str, Any], external: dict[str, Any], expected: str) -> None:
    found = checker.problems_for("prod", default, external, EXTERNAL)
    assert found, "a wrong shape must be reported"
    assert all(line.startswith("prod:") for line in found)
    assert any(expected in line for line in found), found


def test_the_checker_reports_a_missing_snapshot_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    default_dir, external_dir = tmp_path / "default", tmp_path / "external"
    default_dir.mkdir()
    external_dir.mkdir()
    for environment in ("dev", "test"):  # prod is missing in both
        (default_dir / f"{environment}.json").write_text(json.dumps(good_default()), encoding="utf-8")
        (external_dir / f"{environment}.json").write_text(json.dumps(good_external()), encoding="utf-8")
    # The default snapshots hold ag-fabricops-dev for every environment here, which is fine for the checker's purpose.
    assert checker.main(["check", str(default_dir), str(external_dir), EXTERNAL]) == 1
    assert "prod: no snapshot" in capsys.readouterr().out
    (default_dir / "prod.json").write_text(json.dumps(good_default()), encoding="utf-8")
    (external_dir / "prod.json").write_text(json.dumps(good_external()), encoding="utf-8")
    assert checker.main(["check", str(default_dir), str(external_dir), EXTERNAL]) == 0
    assert checker.main(["check"]) == 2  # wrong usage
