#!/usr/bin/env python3
"""Summarise `az deployment group what-if --no-pretty-print` output as Markdown, WITHOUT property values.

    python scripts/whatif_summary.py whatif.json [environment] > summary.md

The summary lists every resource that would change and the property paths that would change. It never prints a
property value: the template reads keys with listKeys(), so a what-if payload can contain storage and workspace keys.
Used by .github/workflows/deploy-environment.yml, which publishes only this summary.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any

QUIET = {"NoChange", "Ignore"}


def _paths(deltas: list[dict[str, Any]], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten the property delta tree into (path, change type) pairs. Values are never read."""
    found: list[tuple[str, str]] = []
    for delta in deltas:
        path = f"{prefix}.{delta['path']}" if prefix and delta.get("path") else delta.get("path", prefix)
        children = delta.get("children") or []
        if children:
            found.extend(_paths(children, path))
        else:
            found.append((path, str(delta.get("propertyChangeType", "?"))))
    return found


def summarise(result: dict[str, Any], environment: str = "") -> str:
    changes: list[dict[str, Any]] = result.get("changes") or result.get("properties", {}).get("changes") or []
    counts = Counter(str(change.get("changeType", "?")) for change in changes)
    lines = [f"### Bicep what-if ({environment})" if environment else "### Bicep what-if", ""]
    if not changes:
        lines.append("No changes reported.")
    else:
        lines.append(", ".join(f"{kind}: {count}" for kind, count in sorted(counts.items())))
        lines.append("")
        for change in changes:
            kind = str(change.get("changeType", "?"))
            if kind in QUIET:
                continue
            lines.append(f"* **{kind}** `{change.get('resourceId', '?')}`")
            for path, property_change in _paths(change.get("delta") or []):
                lines.append(f"    * {property_change}: `{path}`")
    lines += ["", "Property values are intentionally not shown; run the what-if locally to see them."]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        sys.stderr.write(__doc__ or "")
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        sys.stdout.write(summarise(json.load(handle), argv[2] if len(argv) == 3 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
