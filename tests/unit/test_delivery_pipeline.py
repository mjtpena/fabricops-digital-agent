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
    tag = next(i for i, n in enumerate(names) if n.startswith("Tag the image"))
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


def test_cd_build_waits_for_ci_and_ci_is_reusable() -> None:
    jobs = _load(WORKFLOWS / "cd.yml")["jobs"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert "ci" in jobs["build"]["needs"], "nothing is built, pushed or deployed unless CI passed"
    triggers = _load(WORKFLOWS / "ci.yml")[True]  # YAML parses the key `on` as True
    assert "workflow_call" in triggers
    # CD runs CI for pushes to main and to dry-run branches, so CI's own push trigger must skip exactly those.
    assert triggers["push"]["branches-ignore"] == ["main", "cd-dryrun/**"]
    assert {"pull_request", "schedule"} <= set(triggers)


def _cancels(expression: str, event: str, ref: str) -> bool:
    """Evaluate the small && expression used for `cancel-in-progress` (anything else fails the test loudly)."""
    body = expression.strip().removeprefix("${{").removesuffix("}}").strip()
    for clause in (part.strip() for part in body.split("&&")):
        if match := re.fullmatch(r"github\.(event_name|ref) != '([^']+)'", clause):
            allowed = {"event_name": event, "ref": ref}[match[1]] != match[2]
        elif match := re.fullmatch(r"!startsWith\(github\.ref, '([^']+)'\)", clause):
            allowed = not ref.startswith(match[1])
        else:
            raise AssertionError(f"unsupported clause in cancel-in-progress: {clause}")
        if not allowed:
            return False
    return True


def test_ci_concurrency_never_cancels_main_dry_run_or_scheduled_runs() -> None:
    concurrency = _load(WORKFLOWS / "ci.yml")["concurrency"]
    assert "github.event_name" in concurrency["group"], "the weekly run and pushes must not share a group"
    cancel = str(concurrency["cancel-in-progress"])
    # Never cancelled: main, dry-run branches (CI runs inside CD for both), the weekly schedule.
    for event, ref in [
        ("push", "refs/heads/main"),
        ("push", "refs/heads/cd-dryrun/ws4"),
        ("schedule", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/cd-dryrun/other/deep"),
    ]:
        assert not _cancels(cancel, event, ref), f"{event} on {ref} must not be cancelled by a later run"
    # Superseded work on pull requests and feature branches is cancelled to save minutes.
    for event, ref in [("pull_request", "refs/pull/6/merge"), ("push", "refs/heads/feature/x")]:
        assert _cancels(cancel, event, ref), f"{event} on {ref} should be cancellable"


def test_scan_gates_fail_on_unfixed_findings_too() -> None:
    gates = 0
    for name in ("ci.yml", "cd.yml"):
        for step in _steps(_load(WORKFLOWS / name)):
            if (
                str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
                and step["with"].get("exit-code") == "1"
            ):
                gates += 1
                assert step["with"]["ignore-unfixed"] is False, f"{name}: the gate must not ignore unfixed findings"
                assert step["with"]["severity"] == "HIGH,CRITICAL"
    assert gates == 2, "one gate in CI (local build) and one in CD (pushed digest)"


def test_branch_images_never_carry_the_bare_commit_sha_tag() -> None:
    text = (WORKFLOWS / "cd.yml").read_text(encoding="utf-8")
    assert '"$GITHUB_EVENT_NAME" == "push" && "$GITHUB_REF" == "refs/heads/main"' in text
    assert "IMAGE_TAG=dryrun-${GITHUB_SHA}" in text
    assert "IMAGE_TAG=${GITHUB_SHA}" in text
    assert "Tags are not moved" in text


def test_release_has_its_own_environment_and_takes_only_successful_main_runs() -> None:
    release = _load(WORKFLOWS / "release.yml")
    assert release["jobs"]["publish"]["environment"] == "release"
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "environment: dev" not in text
    assert "--branch main" in text
    assert "--status success" in text
    assert "--event push" in text
    assert "release_tools.py fetch" in text, "evidence is fetched one artifact at a time by the tested helper"
    assert "--name image-digest --name sbom" not in text


def test_whatif_has_no_access_to_the_shared_registry_resource_group() -> None:
    text = (WORKFLOWS / "whatif.yml").read_text(encoding="utf-8")
    assert 'ACR_RESOURCE_ID: ""' in text
    assert "vars.ACR_RESOURCE_ID" not in text
    assert "containerapp" not in text.replace("Microsoft.App/containerApps", ""), "no CLI extension needed"


def test_smoke_test_checks_the_deployed_revision() -> None:
    env = next(s for s in _steps(_load(WORKFLOWS / "deploy-environment.yml")) if s.get("name") == "Smoke test")["env"]
    assert env["SMOKE_EXPECTED_IMAGE"] == "${{ inputs.image }}"
    assert {"SMOKE_CONTAINER_APP", "SMOKE_RESOURCE_GROUP"} <= set(env)
    assert env["SMOKE_HEALTH_AUTHENTICATED"].endswith("|| 'false' }}"), "/health is anonymous by default"


def test_dockerfile_uses_one_digest_pinned_non_dockerhub_base_and_asserts_pip_is_gone() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    bases = re.findall(r"^FROM (\S+)", text, re.MULTILINE)
    assert len(bases) == 2
    assert bases[0] == bases[1], "builder and runtime must use the same reference"
    assert re.search(r"@sha256:[0-9a-f]{64}$", bases[0])
    assert bases[0].startswith("mcr.microsoft.com/"), "MCR: no anonymous pull rate limit"
    assert "pip is still installed" in text, "the build fails if pip can still be run"
    assert "site-packages/pip" not in text, "no hard-coded interpreter paths"


def test_oidc_subjects_in_the_docs_name_the_repository_owner() -> None:
    for relative in ("docs/delivery.md", "docs/adr/0016-delivery-pipeline.md", ".github/workflows/cd.yml"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "repo:mjtpe/" not in text, f"{relative}: the owner is mjtpena"


def test_trivy_exceptions_are_justified_and_expire_within_sixty_days() -> None:
    import datetime as dt

    ignore = _load(ROOT / ".trivyignore.yaml")
    today = dt.datetime.now(dt.UTC).date()
    for entry in ignore["vulnerabilities"]:
        assert entry["id"]
        assert str(entry["statement"]).strip(), (
            f"{entry['id']}: a statement naming the owner and the reason is required"
        )
        expires = entry["expired_at"]
        assert expires >= today, f"{entry['id']}: expired on {expires}; remove it or renew it with a fresh review"
        assert expires <= today + dt.timedelta(days=60), f"{entry['id']}: expires more than 60 days ahead"


LEGACY_SUBJECT = re.compile(r"repo:[\w.-]+/[\w.-]+")  # repo:owner/name, the pre-immutable form
IMMUTABLE_SUBJECT = re.compile(r"repo:[\w.-]+@\d+/[\w.-]+@\d+(?::|$)")  # repo:owner@<id>/name@<id>:...
SUBJECT_FILES = (
    "docs/delivery.md",
    "docs/adr/0016-delivery-pipeline.md",
    ".github/workflows/cd.yml",
    ".github/workflows/deploy-environment.yml",
    ".github/workflows/whatif.yml",
    ".github/workflows/release.yml",
)


@pytest.mark.parametrize("relative", SUBJECT_FILES)
def test_no_oidc_subject_uses_the_legacy_owner_name_form(relative: str) -> None:
    """Repositories with immutable subject claims never match `repo:owner/name:...`: every login would fail."""
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert not LEGACY_SUBJECT.search(text), f"{relative}: legacy subject form {LEGACY_SUBJECT.search(text)}"
    assert "repo:$REPO" not in text.replace("SUBJECT_PREFIX:=repo:${REPO}", ""), (
        f"{relative}: build subjects from $SUBJECT_PREFIX"
    )


def test_documented_subjects_use_the_immutable_prefix_and_cover_every_credential() -> None:
    text = (ROOT / "docs" / "delivery.md").read_text(encoding="utf-8")
    subjects = [match.group(0) + rest for match, rest in _subjects(text)]
    assert subjects, "docs/delivery.md documents no subject"
    for subject in subjects:
        assert IMMUTABLE_SUBJECT.match(subject), f"{subject}: not the immutable owner@id/repo@id form"
    documented = " ".join(subjects)
    for suffix in (
        ":ref:refs/heads/main",
        ":environment:dev",
        ":environment:test",
        ":environment:prod",
        ":environment:release",
        ":pull_request",
        ":ref:refs/heads/cd-dryrun/ws4",
    ):
        assert suffix in documented, f"docs/delivery.md does not document the subject ending {suffix}"
    # The prefix is read from GitHub, not typed, and the docs say how and why.
    assert "sub_claim_prefix" in text
    assert "oidc/customization/sub" in text
    assert "AADSTS70021" in text


def _subjects(text: str) -> list[tuple[re.Match[str], str]]:
    """Every literal `repo:...` subject in the document with the `:suffix` that follows it."""
    found = []
    for match in re.finditer(r"repo:[\w.@/-]+", text):
        if match.group(0).startswith("repo:<") or match.group(0) in {"repo:$REPO"}:
            continue
        tail = re.match(r":[\w:/.*<>-]+", text[match.end() :])
        found.append((match, tail.group(0) if tail else ""))
    return found


def test_environments_are_verified_before_any_environment_credential_is_created() -> None:
    text = (ROOT / "docs" / "delivery.md").read_text(encoding="utf-8")
    environments = text.index("### 3. GitHub environments")
    identities = text.index("### 4. Entra identities")
    assert environments < identities
    assert "Do this before section 4" in text
    first_environment_credential = min(
        text.index(marker)
        for marker in ('"$SUBJECT_PREFIX:environment:', "SUBJECT_PREFIX:environment:dev")
        if marker in text
    )
    assert first_environment_credential > environments


def test_required_status_checks_are_pinned_to_the_github_actions_app() -> None:
    text = (ROOT / "docs" / "delivery.md").read_text(encoding="utf-8")
    assert text.count('"integration_id": 15368') == 6, "all six required checks name the GitHub Actions app"
    assert '"context": "' in text
    assert re.search(r'"context": "[^"]+" \}', text) is None, "a required check without integration_id"
    assert "refs/heads/cd-dryrun/**" in text, "a ruleset restricts who can create dry-run branches"
