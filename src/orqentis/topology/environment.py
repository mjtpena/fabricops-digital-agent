"""Environment classification: declared overrides first, naming conventions second.

PROD, TEST and DEV drive incident severity and policy rules such as ``POL-002``, so a wrong guess is
costly. Naming conventions are only a heuristic (``Sales [PROD]``, ``sales-prd``, ``Finance UAT``), so
platform teams can declare the truth in the contracts file (``environments``, see
:class:`~orqentis.domain.estate.EnvironmentDeclaration`). A declaration always beats inference.

Precedence for a workspace: a declaration by ``workspace_id``, else the first matching
``workspace_name_pattern`` (in file order), else inference from the display name. An item takes the
environment of its workspace unless an ``item_id`` declaration says otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from orqentis.domain.common import Environment
from orqentis.domain.estate import EnvironmentDeclaration

_PROD_TOKENS = frozenset({"prod", "production", "prd"})
_TEST_TOKENS = frozenset({"test", "testing", "tst", "uat", "qa", "sit", "preprod", "staging", "stg"})
_DEV_TOKENS = frozenset({"dev", "development", "develop"})


def infer_environment(name: str | None) -> Environment:
    """Environment from naming conventions such as ``Sales [PROD]``, ``sales-prod`` or ``Sales UAT``.

    The last recognised token wins (suffix convention). ``Non-Prod`` / ``Pre-Prod`` are not PROD.
    """
    if not name:
        return Environment.UNKNOWN
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]+", name.lower()):
        if tokens and tokens[-1] in ("non", "pre") and token in _PROD_TOKENS:
            tokens[-1] += "prod"
        else:
            tokens.append(token)
    for token in reversed(tokens):
        if token in _PROD_TOKENS:
            return Environment.PROD
        if token in _TEST_TOKENS:
            return Environment.TEST
        if token in _DEV_TOKENS:
            return Environment.DEV
    return Environment.UNKNOWN


def stage_environment(name: str | None, index: int, count: int) -> Environment:
    """Stage environment: an explicit name wins, else first=DEV, last=PROD, anything between=TEST."""
    named = infer_environment(name)
    if named is not Environment.UNKNOWN or count <= 1:
        return named
    if index == 0:
        return Environment.DEV
    if index == count - 1:
        return Environment.PROD
    return Environment.TEST


def name_matches(pattern: str, name: str) -> bool:
    """Case-insensitive match of ``name`` against ``pattern``; ``*`` is any run, ``?`` any one character.

    Every other character is literal (``[PROD]`` is not a character class), and the matcher is the linear
    two-pointer algorithm, so a hostile pattern or workspace name cannot cause pathological backtracking.
    """
    p, n = pattern.casefold(), name.casefold()
    pi = ni = 0
    star = -1
    resume = 0
    while ni < len(n):
        if pi < len(p) and p[pi] != "*" and (p[pi] == "?" or p[pi] == n[ni]):
            pi += 1
            ni += 1
        elif pi < len(p) and p[pi] == "*":
            star, resume = pi, ni
            pi += 1
        elif star != -1:
            resume += 1
            ni = resume
            pi = star + 1
        else:
            return False
    while pi < len(p) and p[pi] == "*":
        pi += 1
    return pi == len(p)


class EnvironmentSource(StrEnum):
    """Where a resolved environment came from (recorded in workspace metadata for audit)."""

    DECLARED_ID = "declared_id"
    DECLARED_PATTERN = "declared_pattern"
    INFERRED = "inferred"
    INHERITED = "inherited"  # an item taking its workspace's environment
    DECLARED_ITEM = "declared_item"


@dataclass(frozen=True)
class ResolvedEnvironment:
    environment: Environment
    source: EnvironmentSource


class EnvironmentResolver:
    """Resolves workspace and item environments from declarations, falling back to name inference."""

    def __init__(self, declarations: Sequence[EnvironmentDeclaration] = ()) -> None:
        self._by_workspace_id: dict[str, Environment] = {}
        self._by_item_id: dict[str, Environment] = {}
        self._patterns: list[tuple[str, Environment]] = []
        for declaration in declarations:
            # First declaration wins on a duplicate selector; `contracts validate` reports duplicates.
            if declaration.workspace_id is not None:
                self._by_workspace_id.setdefault(declaration.workspace_id, declaration.environment)
            elif declaration.item_id is not None:
                self._by_item_id.setdefault(declaration.item_id, declaration.environment)
            elif declaration.workspace_name_pattern is not None:
                self._patterns.append((declaration.workspace_name_pattern, declaration.environment))

    @property
    def has_declarations(self) -> bool:
        return bool(self._by_workspace_id or self._by_item_id or self._patterns)

    def resolve_workspace(self, workspace_id: str, display_name: str | None) -> ResolvedEnvironment:
        if (declared := self._by_workspace_id.get(workspace_id)) is not None:
            return ResolvedEnvironment(declared, EnvironmentSource.DECLARED_ID)
        if display_name:
            for pattern, environment in self._patterns:
                if name_matches(pattern, display_name):
                    return ResolvedEnvironment(environment, EnvironmentSource.DECLARED_PATTERN)
        return ResolvedEnvironment(infer_environment(display_name), EnvironmentSource.INFERRED)

    def workspace(self, workspace_id: str, display_name: str | None) -> Environment:
        return self.resolve_workspace(workspace_id, display_name).environment

    def resolve_item(self, item_id: str, workspace_environment: Environment) -> ResolvedEnvironment:
        if (declared := self._by_item_id.get(item_id)) is not None:
            return ResolvedEnvironment(declared, EnvironmentSource.DECLARED_ITEM)
        return ResolvedEnvironment(workspace_environment, EnvironmentSource.INHERITED)

    def item(self, item_id: str, workspace_environment: Environment) -> Environment:
        return self.resolve_item(item_id, workspace_environment).environment
