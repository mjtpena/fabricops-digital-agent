"""The offline Bicep diff (scripts/bicep_snapshot_diff.py): what a pull request reviewer sees per environment."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bicep_snapshot_diff", ROOT / "scripts" / "bicep_snapshot_diff.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


diff = _load()

APP: dict[str, Any] = {
    "type": "Microsoft.App/containerApps",
    "name": "ca-fabricops-dev",
    "properties": {
        "template": {
            "containers": [
                {
                    "name": "orqentis",
                    "image": "registry.invalid/orqentis-fabricops@sha256:0",
                    "env": [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}],
                }
            ],
            "scale": {"minReplicas": 1, "maxReplicas": 1},
        }
    },
}
IDENTITY: dict[str, Any] = {
    "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
    "name": "id-fabricops-dev-reader",
}
SNAPSHOT: dict[str, Any] = {
    "predictedResources": [APP, IDENTITY],
    "outputs": {"containerAppName": {"type": "string", "value": "ca-fabricops-dev"}},
}


def _write(directory: Path, **snapshots: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for environment, snapshot in snapshots.items():
        (directory / f"{environment}.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return directory


def test_identical_snapshots_report_no_change() -> None:
    lines = diff.diff_environment("dev", SNAPSHOT, copy.deepcopy(SNAPSHOT))
    assert "No change in what this environment would deploy." in lines


def test_property_changes_show_the_path_and_both_values() -> None:
    head = copy.deepcopy(SNAPSHOT)
    head["predictedResources"][0]["properties"]["template"]["scale"]["maxReplicas"] = 3
    text = "\n".join(diff.diff_environment("prod", SNAPSHOT, head))
    assert "`properties.template.scale.maxReplicas`: `1` → `3`" in text
    assert "0 added, 0 removed, 1 property change(s)." in text


def test_env_vars_are_matched_by_name_not_by_position() -> None:
    head = copy.deepcopy(SNAPSHOT)
    env = head["predictedResources"][0]["properties"]["template"]["containers"][0]["env"]
    env.reverse()  # same variables, different order: not a change
    env.append({"name": "C", "value": "3"})
    text = "\n".join(diff.diff_environment("dev", SNAPSHOT, head))
    assert "env[C].value` = `3`" in text
    assert "env[A]" not in text
    assert "env[B]" not in text


def test_added_and_removed_resources_are_listed() -> None:
    head = copy.deepcopy(SNAPSHOT)
    head["predictedResources"] = [APP, {"type": "Microsoft.Insights/components", "name": "appi-fabricops-dev"}]
    text = "\n".join(diff.diff_environment("dev", SNAPSHOT, head))
    assert "added `Microsoft.Insights/components appi-fabricops-dev`" in text
    assert "removed `Microsoft.ManagedIdentity/userAssignedIdentities id-fabricops-dev-reader`" in text


def test_output_changes_are_reported() -> None:
    head = copy.deepcopy(SNAPSHOT)
    head["outputs"]["containerAppName"]["value"] = "ca-renamed"
    text = "\n".join(diff.diff_environment("dev", SNAPSHOT, head))
    assert "output `containerAppName.value`: `ca-fabricops-dev` → `ca-renamed`" in text


def test_a_new_environment_lists_everything_as_added() -> None:
    text = "\n".join(diff.diff_environment("test", None, SNAPSHOT))
    assert "New environment: 2 resource(s) would be deployed." in text
    assert "added `Microsoft.App/containerApps ca-fabricops-dev`" in text


def test_a_missing_head_snapshot_is_called_out() -> None:
    assert "No snapshot for this environment on the head" in "\n".join(diff.diff_environment("prod", SNAPSHOT, None))


def test_long_values_and_long_change_lists_are_truncated() -> None:
    head = copy.deepcopy(SNAPSHOT)
    head["predictedResources"][0]["properties"]["template"]["containers"][0]["image"] = "x" * 1000
    head["predictedResources"][0]["properties"]["extra"] = {
        f"k{i}": i for i in range(diff.MAX_CHANGES_PER_ENVIRONMENT + 20)
    }
    text = "\n".join(diff.diff_environment("dev", SNAPSHOT, head))
    assert "x" * 1000 not in text
    assert "…" in text
    assert "more property change(s)" in text


def test_render_covers_all_three_environments_and_tolerates_missing_files(tmp_path: Path) -> None:
    base = _write(tmp_path / "base", dev=SNAPSHOT, prod=SNAPSHOT)
    head = _write(tmp_path / "head", dev=SNAPSHOT, test=SNAPSHOT, prod=SNAPSHOT)
    markdown = diff.render(base, head)
    assert markdown.startswith("## Bicep diff (offline)")
    for environment in ("dev", "test", "prod"):
        assert f"### {environment}" in markdown
    assert "New environment" in markdown, "test did not exist on the base"


def test_the_command_line_prints_utf8_markdown(tmp_path: Path, capsys: Any) -> None:
    base = _write(tmp_path / "base", dev=SNAPSHOT)
    changed = copy.deepcopy(SNAPSHOT)
    changed["predictedResources"][0]["properties"]["template"]["scale"]["maxReplicas"] = 2
    head = _write(tmp_path / "head", dev=changed)
    assert diff.main(["diff", str(base), str(head)]) == 0
    assert "→" in capsys.readouterr().out
    assert diff.main(["diff"]) == 2
