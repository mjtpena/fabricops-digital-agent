"""Environment classification: declared overrides first, naming conventions second.

PROD, TEST and DEV drive incident severity and policy rules such as ``POL-002``, so a wrong guess is
costly. Naming conventions are only a heuristic (``Sales [PROD]``, ``sales-prd``, ``Finance UAT``), so
platform teams can declare the truth in the contracts file (``environments``, see
:class:`~orqentis.domain.estate.EnvironmentDeclaration`). A declaration always beats inference.

Precedence for a workspace: a declaration by ``workspace_id``, else the first matching
``workspace_name_pattern`` (in file order), else inference from the display name. An item takes the
environment of its workspace unless an ``item_id`` declaration says otherwise; a capacity is declared by
``capacity_id`` and otherwise inferred from its name.

Only *enforced* declarations count (reviewed, not ``inferred``, not ``UNKNOWN``; see
:attr:`~orqentis.domain.estate.EnvironmentDeclaration.enforced`). A draft entry never overrides what the
name says; the result then carries :attr:`EnvironmentSource.DRAFT_IGNORED` so the audit trail can tell.
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


_RANK = {Environment.DEV: 0, Environment.TEST: 1, Environment.PROD: 2}


def is_downgrade(before: Environment, after: Environment) -> bool:
    """True when ``before`` is PROD and ``after`` is a lower, known environment (TEST or DEV)."""
    return before is Environment.PROD and after in _RANK and _RANK[after] < _RANK[before]


def name_matches(pattern: str, name: str) -> bool:
    """Case-insensitive match of ``name`` against ``pattern``; ``*`` is any run, ``?`` any one character.

    Every other character is literal (``[PROD]`` is not a character class). The matcher is the classic
    two-pointer wildcard algorithm with a single backtrack point: worst case O(len(pattern) * len(name)),
    both bounded (patterns are limited to 128 characters), with no exponential backtracking.
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
    """Where a resolved environment came from (recorded in workspace metadata and the audit trail)."""

    DECLARED_ID = "declared_id"
    DECLARED_PATTERN = "declared_pattern"
    DECLARED_ITEM = "declared_item"
    DECLARED_CAPACITY = "declared_capacity"
    INFERRED = "inferred"  # from the display name
    INHERITED = "inherited"  # an item taking its workspace's environment
    # An unreviewed (`inferred: true`) or UNKNOWN declaration matched but was not enforced, so the
    # environment came from the name (or the workspace) instead.
    DRAFT_IGNORED = "draft_ignored"

    @property
    def declared(self) -> bool:
        return self.value.startswith("declared_")


@dataclass(frozen=True)
class ResolvedEnvironment:
    environment: Environment
    source: EnvironmentSource
    declaration_index: int | None = None  # index of the declaration that decided it (declared sources only)


class EnvironmentResolver:
    """Resolves workspace, item and capacity environments from declarations, falling back to name inference."""

    def __init__(self, declarations: Sequence[EnvironmentDeclaration] = ()) -> None:
        self._by_workspace_id: dict[str, tuple[Environment, int]] = {}
        self._by_item_id: dict[str, tuple[Environment, int]] = {}
        self._by_capacity_id: dict[str, tuple[Environment, int]] = {}
        self._patterns: list[tuple[str, Environment, int]] = []
        # Selectors of declarations that exist but are not enforced, to explain a fall-through.
        self._ignored_workspace_ids: set[str] = set()
        self._ignored_item_ids: set[str] = set()
        self._ignored_capacity_ids: set[str] = set()
        self._ignored_patterns: list[str] = []
        for index, declaration in enumerate(declarations):
            # The first enforced declaration wins on a duplicate selector; `contracts validate` reports duplicates.
            if declaration.workspace_id is not None:
                if declaration.enforced:
                    self._by_workspace_id.setdefault(declaration.workspace_id, (declaration.environment, index))
                else:
                    self._ignored_workspace_ids.add(declaration.workspace_id)
            elif declaration.item_id is not None:
                if declaration.enforced:
                    self._by_item_id.setdefault(declaration.item_id, (declaration.environment, index))
                else:
                    self._ignored_item_ids.add(declaration.item_id)
            elif declaration.capacity_id is not None:
                if declaration.enforced:
                    self._by_capacity_id.setdefault(declaration.capacity_id, (declaration.environment, index))
                else:
                    self._ignored_capacity_ids.add(declaration.capacity_id)
            elif declaration.workspace_name_pattern is not None:
                if declaration.enforced:
                    self._patterns.append((declaration.workspace_name_pattern, declaration.environment, index))
                else:
                    self._ignored_patterns.append(declaration.workspace_name_pattern)

    @property
    def has_declarations(self) -> bool:
        return bool(self._by_workspace_id or self._by_item_id or self._by_capacity_id or self._patterns)

    def _ignored_workspace(self, workspace_id: str, display_name: str | None) -> bool:
        return workspace_id in self._ignored_workspace_ids or (
            display_name is not None and any(name_matches(p, display_name) for p in self._ignored_patterns)
        )

    def resolve_declared_workspace(self, workspace_id: str, display_name: str | None) -> ResolvedEnvironment | None:
        """The enforced declaration for a workspace, if there is one (no name inference)."""
        if (by_id := self._by_workspace_id.get(workspace_id)) is not None:
            return ResolvedEnvironment(by_id[0], EnvironmentSource.DECLARED_ID, by_id[1])
        if display_name:
            for pattern, environment, index in self._patterns:
                if name_matches(pattern, display_name):
                    return ResolvedEnvironment(environment, EnvironmentSource.DECLARED_PATTERN, index)
        return None

    def resolve_workspace(self, workspace_id: str, display_name: str | None) -> ResolvedEnvironment:
        if (declared := self.resolve_declared_workspace(workspace_id, display_name)) is not None:
            return declared
        source = (
            EnvironmentSource.DRAFT_IGNORED
            if self._ignored_workspace(workspace_id, display_name)
            else EnvironmentSource.INFERRED
        )
        return ResolvedEnvironment(infer_environment(display_name), source)

    def workspace(self, workspace_id: str, display_name: str | None) -> Environment:
        return self.resolve_workspace(workspace_id, display_name).environment

    def resolve_item(self, item_id: str, workspace_environment: Environment) -> ResolvedEnvironment:
        if (declared := self._by_item_id.get(item_id)) is not None:
            return ResolvedEnvironment(declared[0], EnvironmentSource.DECLARED_ITEM, declared[1])
        source = EnvironmentSource.DRAFT_IGNORED if item_id in self._ignored_item_ids else EnvironmentSource.INHERITED
        return ResolvedEnvironment(workspace_environment, source)

    def item(self, item_id: str, workspace_environment: Environment) -> Environment:
        return self.resolve_item(item_id, workspace_environment).environment

    def resolve_capacity(self, capacity_id: str, display_name: str | None) -> ResolvedEnvironment:
        if (declared := self._by_capacity_id.get(capacity_id)) is not None:
            return ResolvedEnvironment(declared[0], EnvironmentSource.DECLARED_CAPACITY, declared[1])
        source = (
            EnvironmentSource.DRAFT_IGNORED if capacity_id in self._ignored_capacity_ids else EnvironmentSource.INFERRED
        )
        return ResolvedEnvironment(infer_environment(display_name), source)
