"""Delivery pipeline invariants (ADR-0016): workflows stay pinned, secret-free and promote by digest."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.yml"))
SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow.get("jobs", {}).values() for step in job.get("steps", [])]


def test_workflows_exist() -> None:
    names = {path.name for path in WORKFLOW_FILES}
    assert {"ci.yml", "cd.yml", "release.yml", "whatif.yml", "deploy-environment.yml"} <= names


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_action_is_pinned_by_commit_sha(path: Path) -> None:
    workflow = _load(path)
    uses = [job["uses"] for job in workflow["jobs"].values() if "uses" in job]
    uses += [step["uses"] for step in _steps(workflow) if "uses" in step]
    for reference in uses:
        if reference.startswith("./"):
            continue  # a reusable workflow in this repository
        assert SHA_PIN.match(reference), f"{path.name}: {reference} is not pinned by a 40-character commit SHA"
        assert not reference.startswith("docker://")


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_pinned_actions_carry_their_version_in_a_comment(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.search(r"uses:\s+(\S+@[0-9a-f]{40})(.*)$", line)
        if match:
            assert re.match(r"\s+# v\d+(\.\d+)*", match.group(2)), (
                f"{path.name}: no '# vX.Y.Z' comment on: {line.strip()}"
            )


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_runners_are_pinned(path: Path) -> None:
    for name, job in _load(path)["jobs"].items():
        if "runs-on" in job:
            assert job["runs-on"] == "ubuntu-24.04", f"{path.name}:{name} must pin ubuntu-24.04, not {job['runs-on']}"


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_no_workflow_uses_a_secret(path: Path) -> None:
    """Azure access is OIDC with identifiers held in variables; GITHUB_TOKEN is the only secret-context value."""
    text = path.read_text(encoding="utf-8")
    for reference in re.findall(r"secrets\.([A-Za-z0-9_]+)", text):
        assert reference == "GITHUB_TOKEN", f"{path.name} references secrets.{reference}"
    assert "secrets: inherit" not in text
    assert not re.search(r"client-secret|AZURE_CREDENTIALS|password:", text, re.IGNORECASE)


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_default_permissions_are_read_only(path: Path) -> None:
    workflow = _load(path)
    assert workflow["permissions"] == {"contents": "read"}


def test_id_token_is_granted_only_to_jobs_that_log_in() -> None:
    for path in WORKFLOW_FILES:
        for name, job in _load(path)["jobs"].items():
            if job.get("permissions", {}).get("id-token") == "write":
                logs_in = any(
                    str(step.get("uses", "")).startswith("azure/login@") for step in job.get("steps", [])
                ) or str(job.get("uses", "")).startswith("./.github/workflows/deploy-environment.yml")
                assert logs_in, f"{path.name}:{name} has id-token: write but never logs in"


def test_cd_promotes_the_same_digest_through_gated_environments() -> None:
    jobs = _load(WORKFLOWS / "cd.yml")["jobs"]
    assert "environment" not in jobs["build"], "the build job must not run under a deployment environment"
    expected = {
        "deploy-dev": ("dev", "build"),
        "promote-test": ("test", "deploy-dev"),
        "promote-prod": ("prod", "promote-test"),
    }
    for name, (environment, previous) in expected.items():
        job = jobs[name]
        assert job["uses"] == "./.github/workflows/deploy-environment.yml"
        assert job["with"]["environment"] == environment
        assert job["with"]["image"] == "${{ needs.build.outputs.image }}", (
            f"{name} must deploy the image the build job pushed"
        )
        assert previous in job["needs"]
    # The only image output comes from the digest returned by the one build step.
    build_steps = jobs["build"]["steps"]
    assert sum(1 for s in build_steps if str(s.get("uses", "")).startswith("docker/build-push-action@")) == 1


def test_cd_gate_comes_before_tagging() -> None:
    steps = _load(WORKFLOWS / "cd.yml")["jobs"]["build"]["steps"]
    names = [step.get("name", "") for step in steps]
    gate = next(i for i, n in enumerate(names) if n.startswith("Scan gate"))
    tag = next(i for i, n in enumerate(names) if n.startswith("Tag with the git SHA"))
    push = next(i for i, n in enumerate(names) if n.startswith("Build and push"))
    assert push < gate < tag
    gate_step = steps[gate]["with"]
    assert gate_step["exit-code"] == "1"
    assert gate_step["severity"] == "HIGH,CRITICAL"
    assert gate_step["trivyignores"] == ".trivyignore.yaml"
    build_with = steps[push]["with"]
    assert "push-by-digest=true" in build_with["outputs"], "an unscanned image must not receive a tag"
    assert build_with["provenance"] == "mode=max"


def test_dry_run_can_skip_every_deployment() -> None:
    cd = _load(WORKFLOWS / "cd.yml")
    triggers = cd[True]  # YAML parses the key `on` as True
    assert triggers["push"]["branches"] == ["main", "cd-dryrun/**"]
    assert triggers["workflow_dispatch"]["inputs"]["deploy"]["type"] == "boolean"
    condition = cd["jobs"]["deploy-dev"]["if"]
    # A push deploys only from main (never from a cd-dryrun branch); a manual run deploys only when asked to.
    assert "github.ref == 'refs/heads/main'" in condition
    assert "inputs.deploy" in condition
    for later in ("promote-test", "promote-prod"):
        assert "if" not in cd["jobs"][later], "later stages follow their predecessor and need no condition of their own"


def test_deploy_workflow_uses_the_environment_it_is_given() -> None:
    job = _load(WORKFLOWS / "deploy-environment.yml")["jobs"]["deploy"]
    assert job["environment"] == "${{ inputs.environment }}"
    text = (WORKFLOWS / "deploy-environment.yml").read_text(encoding="utf-8")
    assert "az deployment group create" in text
    assert "what-if" not in text


def test_whatif_workflow_never_deploys() -> None:
    text = (WORKFLOWS / "whatif.yml").read_text(encoding="utf-8")
    assert "az deployment group what-if" in text
    assert "deployment group create" not in text
    assert "environment:" not in text.split("jobs:", 1)[1], "the what-if job must present the pull_request subject"


def test_release_workflow_triggers_on_version_tags_only() -> None:
    release = _load(WORKFLOWS / "release.yml")
    assert release[True] == {"push": {"tags": ["v*.*.*"]}}


def test_dependabot_covers_actions_and_docker() -> None:
    ecosystems = {entry["package-ecosystem"] for entry in _load(ROOT / ".github" / "dependabot.yml")["updates"]}
    assert {"github-actions", "docker"} <= ecosystems


@pytest.mark.parametrize("environment", ["dev", "test", "prod"])
def test_environment_parameter_files_exist_and_hold_no_secrets(environment: str) -> None:
    text = (ROOT / "infra" / "bicep" / "env" / f"{environment}.bicepparam").read_text(encoding="utf-8")
    assert "using '../main.bicep'" in text
    assert f"param deploymentEnvironment = '{environment}'" in text
    assert "readEnvironmentVariable('CONTAINER_IMAGE')" in text
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    assert not re.search(r"(?i)password|secret|accountkey|sharedkey|sharedaccesssignature", code)
    guid = r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    assert not re.search(guid, code), "no literal ids"


def test_production_writes_stay_disabled() -> None:
    text = (ROOT / "infra" / "bicep" / "env" / "prod.bicepparam").read_text(encoding="utf-8")
    assert re.search(r"^param enableWriteActions = false$", text, re.MULTILINE)
    assert "enableWriteActions = true" not in text


def _load_summary() -> Any:
    spec = importlib.util.spec_from_file_location("whatif_summary", ROOT / "scripts" / "whatif_summary.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RESOURCE_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.App/managedEnvironments/cae-x/storages/d"


def test_whatif_summary_lists_changes_and_never_prints_values() -> None:
    secret_value = "AccountKey-should-never-be-printed"
    result = {
        "status": "Succeeded",
        "changes": [
            {
                "resourceId": RESOURCE_ID,
                "changeType": "Modify",
                "after": {"properties": {"azureFile": {"accountKey": secret_value}}},
                "delta": [
                    {
                        "path": "properties.azureFile",
                        "propertyChangeType": "Modify",
                        "children": [
                            {
                                "path": "accountKey",
                                "propertyChangeType": "Modify",
                                "before": "old-key",
                                "after": secret_value,
                            }
                        ],
                    }
                ],
            },
            {
                "resourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/st",
                "changeType": "NoChange",
            },
        ],
    }
    summary = _load_summary().summarise(result)
    assert "Modify: 1" in summary
    assert "NoChange: 1" in summary
    assert "`properties.azureFile.accountKey`" in summary
    assert secret_value not in summary
    assert "old-key" not in summary
    assert "storageAccounts/st" not in summary, "unchanged resources are counted but not listed"


def test_whatif_summary_handles_an_empty_result() -> None:
    assert "No changes reported." in _load_summary().summarise({"changes": []})
