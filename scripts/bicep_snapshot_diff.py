#!/usr/bin/env python3
"""Markdown diff of two sets of offline Bicep snapshots (scripts/bicep-snapshot.sh), one file per environment.

    python scripts/bicep_snapshot_diff.py <base dir> <head dir> > diff.md

Each directory holds ``dev.json``, ``test.json`` and ``prod.json``. The snapshots come from the repository files
alone (template, parameter files and fixed placeholders), so they contain no credentials and no values read from Azure,
and the diff can be shown on a pull request without any. Resources are matched by type and name; a changed resource
lists the property paths that differ with their before and after values. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ENVIRONMENTS = ("dev", "test", "prod")
MAX_CHANGES_PER_ENVIRONMENT = 150
MAX_VALUE_LENGTH = 160


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Leaf paths of a JSON value. Lists of named objects (env vars, secrets) are keyed by name, not by position."""
    if isinstance(value, dict):
        leaves: dict[str, Any] = {}
        for key, item in value.items():
            leaves.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return leaves or {prefix: {}}
    if isinstance(value, list):
        names = [item.get("name") for item in value if isinstance(item, dict)]
        keyed = len(names) == len(value) and all(isinstance(n, str) for n in names) and len(set(names)) == len(names)
        leaves = {}
        for index, item in enumerate(value):
            label = f"[{item['name']}]" if keyed else f"[{index}]"
            leaves.update(flatten(item, f"{prefix}{label}"))
        return leaves or {prefix: []}
    return {prefix: value}


def _show(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= MAX_VALUE_LENGTH else text[: MAX_VALUE_LENGTH - 1] + "…"


def _resources(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f"{r.get('type', '?')} {r.get('name', '?')}": r for r in snapshot.get("predictedResources", [])}


def diff_environment(environment: str, base: dict[str, Any] | None, head: dict[str, Any] | None) -> list[str]:
    lines = [f"### {environment}", ""]
    if head is None:
        missing = "No snapshot for this environment on the head (the parameter file is missing or does not compile)."
        return [*lines, missing, ""]
    head_resources = _resources(head)
    if base is None:
        lines.append(f"New environment: {len(head_resources)} resource(s) would be deployed.")
        lines += [f"* added `{key}`" for key in sorted(head_resources)]
        return [*lines, ""]

    base_resources = _resources(base)
    added = sorted(set(head_resources) - set(base_resources))
    removed = sorted(set(base_resources) - set(head_resources))
    changes: list[str] = []
    for key in sorted(set(head_resources) & set(base_resources)):
        before, after = flatten(base_resources[key]), flatten(head_resources[key])
        for path in sorted(set(before) | set(after)):
            if path not in after:
                changes.append(f"* `{key}`: removed `{path}` (was `{_show(before[path])}`)")
            elif path not in before:
                changes.append(f"* `{key}`: added `{path}` = `{_show(after[path])}`")
            elif before[path] != after[path]:
                changes.append(f"* `{key}`: `{path}`: `{_show(before[path])}` → `{_show(after[path])}`")
    output_before, output_after = flatten(base.get("outputs", {})), flatten(head.get("outputs", {}))
    for path in sorted(set(output_before) | set(output_after)):
        if output_before.get(path) != output_after.get(path):
            changes.append(f"* output `{path}`: `{_show(output_before.get(path))}` → `{_show(output_after.get(path))}`")

    if not (added or removed or changes):
        return [*lines, "No change in what this environment would deploy.", ""]
    lines.append(f"{len(added)} added, {len(removed)} removed, {len(changes)} property change(s).")
    lines += [f"* added `{key}`" for key in added]
    lines += [f"* removed `{key}`" for key in removed]
    lines += changes[:MAX_CHANGES_PER_ENVIRONMENT]
    if len(changes) > MAX_CHANGES_PER_ENVIRONMENT:
        lines.append(f"* … and {len(changes) - MAX_CHANGES_PER_ENVIRONMENT} more property change(s) (see the artifact)")
    return [*lines, ""]


def _load(directory: Path, environment: str) -> dict[str, Any] | None:
    path = directory / f"{environment}.json"
    if not path.is_file():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def render(base_dir: Path, head_dir: Path) -> str:
    lines = [
        "## Bicep diff (offline)",
        "",
        "What each environment would deploy, before and after this pull request, from the repository files alone"
        " (`bicep snapshot`, no Azure access, fixed placeholders for the image, registry and ids). Not a substitute for"
        " the"
        " what-if the deploy job records: that one sees the live resources.",
        "",
    ]
    for environment in ENVIRONMENTS:
        lines += diff_environment(environment, _load(base_dir, environment), _load(head_dir, environment))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write(__doc__ or "")
        return 2
    # The diff contains arrows and ellipses; Markdown for GitHub is UTF-8 whatever the console code page is.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.write(render(Path(argv[1]), Path(argv[2])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
