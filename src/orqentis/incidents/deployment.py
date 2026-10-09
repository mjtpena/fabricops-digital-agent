"""Deterministic comparison of an item's deployment state across DEV / TEST / PROD."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import pairwise

from orqentis.domain.common import Environment
from orqentis.domain.deployment import DeploymentComparison, DeploymentState

_ORDER = [Environment.DEV, Environment.TEST, Environment.PROD]


def compare_deployments(
    item_id: str, states: Sequence[DeploymentState], *, reference_time: datetime, window_hours: int = 72
) -> DeploymentComparison | None:
    own = next((s for s in states if s.item_id == item_id), None)
    if own is None:
        return None
    by_env = {s.environment: s for s in states if s.logical_item_id == own.logical_item_id}
    differences: list[str] = []
    for earlier, later in pairwise(_ORDER):
        a, b = by_env.get(earlier), by_env.get(later)
        if a is None or b is None:
            continue
        if a.commit_id != b.commit_id:
            differences.append(f"{earlier.value} commit {a.commit_id} differs from {later.value} commit {b.commit_id}")
        if a.config_hash != b.config_hash:
            differences.append(f"{earlier.value} config {a.config_hash} differs from {later.value} {b.config_hash}")
    test, prod = by_env.get(Environment.TEST), by_env.get(Environment.PROD)
    drift = bool(test and prod and (test.commit_id != prod.commit_id or test.config_hash != prod.config_hash))
    last_change = (prod or own).deployed_at
    recent = bool(last_change and timedelta(0) <= reference_time - last_change <= timedelta(hours=window_hours))
    return DeploymentComparison(
        logical_item_id=own.logical_item_id,
        item_id=item_id,
        states=sorted(by_env.values(), key=lambda s: _ORDER.index(s.environment) if s.environment in _ORDER else 9),
        drift_detected=drift,
        differences=differences,
        last_production_change=last_change,
        changed_within_window=recent,
        window_hours=window_hours,
    )
