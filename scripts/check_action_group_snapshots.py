#!/usr/bin/env python3
"""Checks, on offline Bicep snapshots (scripts/bicep-snapshot.sh), which action group the alerts notify.

    python scripts/check_action_group_snapshots.py <default dir> <external dir> <external action group id>

``<default dir>`` holds the snapshots of the environments as they are in the repository: the template creates its own
action group and every alert uses it. ``<external dir>`` holds the snapshots taken with ALERT_ACTION_GROUP_ID set: the
template must then create no action group at all, and every alert must use the given one. Exit status 1 and one line per
problem otherwise. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ENVIRONMENTS = ("dev", "test", "prod")
GROUP_TYPE = "Microsoft.Insights/actionGroups"
LOG_ALERT = "Microsoft.Insights/scheduledQueryRules"
METRIC_ALERT = "Microsoft.Insights/metricAlerts"
ALERTS_PER_ENVIRONMENT = 6


def _resources(snapshot: dict[str, Any], *types: str) -> list[dict[str, Any]]:
    return [r for r in snapshot.get("predictedResources", []) if r.get("type") in types]


def _groups_used_by(alert: dict[str, Any]) -> list[str]:
    actions = alert.get("properties", {}).get("actions")
    if alert.get("type") == LOG_ALERT:
        return [str(g) for g in (actions or {}).get("actionGroups", [])]
    return [str(a.get("actionGroupId")) for a in (actions or [])]


def problems_for(
    environment: str, default: dict[str, Any] | None, external: dict[str, Any] | None, external_id: str
) -> list[str]:
    """What is wrong in one environment's pair of snapshots (an empty list: nothing)."""
    if default is None or external is None:
        return [f"{environment}: no snapshot ({'default' if default is None else 'external'})"]
    found: list[str] = []

    groups = _resources(default, GROUP_TYPE)
    alerts = _resources(default, LOG_ALERT, METRIC_ALERT)
    if len(groups) != 1:
        found.append(f"{environment}: expected exactly one action group created by the template, found {len(groups)}")
    if len(alerts) != ALERTS_PER_ENVIRONMENT:
        found.append(f"{environment}: expected {ALERTS_PER_ENVIRONMENT} alert rules, found {len(alerts)}")
    for alert in alerts:
        used = [g.lower() for g in _groups_used_by(alert)]
        own = f"/actiongroups/{groups[0]['name']}".lower() if len(groups) == 1 else None
        if own is None or len(used) != 1 or not used[0].endswith(own):
            found.append(
                f"{environment}: alert {alert.get('name')} does not use the template's own action group: {used}"
            )

    external_groups = _resources(external, GROUP_TYPE)
    external_alerts = _resources(external, LOG_ALERT, METRIC_ALERT)
    if external_groups:
        names = [g.get("name") for g in external_groups]
        found.append(f"{environment}: an action group is still created when ALERT_ACTION_GROUP_ID is set: {names}")
    if len(external_alerts) != ALERTS_PER_ENVIRONMENT:
        found.append(
            f"{environment}: expected {ALERTS_PER_ENVIRONMENT} alert rules with the external group, "
            f"found {len(external_alerts)}"
        )
    for alert in external_alerts:
        used = _groups_used_by(alert)
        if [g.lower() for g in used] != [external_id.lower()]:
            found.append(f"{environment}: alert {alert.get('name')} does not use the external action group: {used}")
    return found


def _load(directory: Path, environment: str) -> dict[str, Any] | None:
    path = directory / f"{environment}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    default_dir, external_dir, external_id = Path(argv[1]), Path(argv[2]), argv[3]
    found: list[str] = []
    for environment in ENVIRONMENTS:
        found += problems_for(
            environment, _load(default_dir, environment), _load(external_dir, environment), external_id
        )
    for line in found:
        print(line)
    if not found:
        print(
            "alerts use the template's action group by default and the separately managed one when "
            "ALERT_ACTION_GROUP_ID is set (dev, test, prod); no action group is created in that case"
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
