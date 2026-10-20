"""Declared environments beat name inference (by workspace id, by name pattern, by item id)."""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from orqentis.domain.common import Environment
from orqentis.domain.estate import EnvironmentDeclaration
from orqentis.topology.environment import EnvironmentResolver, EnvironmentSource, name_matches

PROD, TEST, DEV, UNKNOWN = Environment.PROD, Environment.TEST, Environment.DEV, Environment.UNKNOWN


def decl(environment: Environment, **selector: str) -> EnvironmentDeclaration:
    return EnvironmentDeclaration(environment=environment, **selector)


# ------------------------------------------------------------------------------------------ precedence
def test_declared_workspace_id_beats_name_inference() -> None:
    resolver = EnvironmentResolver([decl(PROD, workspace_id="ws-1")])
    resolved = resolver.resolve_workspace("ws-1", "Sales [DEV]")
    assert (resolved.environment, resolved.source) == (PROD, EnvironmentSource.DECLARED_ID)


def test_declared_name_pattern_beats_name_inference() -> None:
    resolver = EnvironmentResolver([decl(PROD, workspace_name_pattern="Finance *")])
    resolved = resolver.resolve_workspace("ws-9", "Finance Reporting [DEV]")
    assert (resolved.environment, resolved.source) == (PROD, EnvironmentSource.DECLARED_PATTERN)


def test_undeclared_workspace_falls_back_to_inference() -> None:
    resolver = EnvironmentResolver([decl(PROD, workspace_id="ws-1"), decl(DEV, workspace_name_pattern="Lab *")])
    resolved = resolver.resolve_workspace("ws-2", "Marketing [TEST]")
    assert (resolved.environment, resolved.source) == (TEST, EnvironmentSource.INFERRED)
    assert resolver.workspace("ws-3", "Shared Services") is UNKNOWN


def test_without_declarations_it_is_plain_inference() -> None:
    resolver = EnvironmentResolver()
    assert not resolver.has_declarations
    assert resolver.workspace("ws", "Sales [PROD]") is PROD


def test_id_beats_pattern_and_the_first_matching_pattern_wins() -> None:
    resolver = EnvironmentResolver(
        [
            decl(TEST, workspace_name_pattern="Sales*"),
            decl(DEV, workspace_name_pattern="Sales Analytics*"),
            decl(PROD, workspace_id="ws-1"),
        ]
    )
    assert resolver.workspace("ws-1", "Sales Analytics") is PROD  # id first, whatever the file order
    assert resolver.workspace("ws-2", "Sales Analytics") is TEST  # first matching pattern in file order


def test_a_declaration_can_demote_a_workspace_that_looks_like_prod() -> None:
    resolver = EnvironmentResolver([decl(DEV, workspace_id="ws-1")])
    assert resolver.workspace("ws-1", "Sales [PROD]") is DEV


def test_items_inherit_the_workspace_environment_unless_declared() -> None:
    resolver = EnvironmentResolver([decl(DEV, item_id="nb-lab")])
    inherited = resolver.resolve_item("pl-1", PROD)
    declared = resolver.resolve_item("nb-lab", PROD)
    assert (inherited.environment, inherited.source) == (PROD, EnvironmentSource.INHERITED)
    assert (declared.environment, declared.source) == (DEV, EnvironmentSource.DECLARED_ITEM)


def test_first_declaration_wins_on_a_duplicate_selector() -> None:
    resolver = EnvironmentResolver([decl(PROD, workspace_id="ws-1"), decl(DEV, workspace_id="ws-1")])
    assert resolver.workspace("ws-1", None) is PROD


def test_declaration_applies_without_a_display_name() -> None:
    assert EnvironmentResolver([decl(TEST, workspace_id="ws-1")]).workspace("ws-1", None) is TEST
    # A name pattern needs a name: with none the workspace falls back to inference (UNKNOWN).
    assert EnvironmentResolver([decl(TEST, workspace_name_pattern="*")]).workspace("ws-1", None) is UNKNOWN


# ------------------------------------------------------------------------------------------ the model
def test_exactly_one_selector_is_required() -> None:
    with pytest.raises(ValidationError, match="exactly one of"):
        EnvironmentDeclaration(environment=PROD)
    with pytest.raises(ValidationError, match="exactly one of"):
        EnvironmentDeclaration(environment=PROD, workspace_id="a", item_id="b")


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        EnvironmentDeclaration.model_validate({"workspace_id": "a", "environment": "PROD", "env": "PROD"})


# ---------------------------------------------------------------------------------------------- glob
@pytest.mark.parametrize(
    ("pattern", "name", "expected"),
    [
        ("Sales*", "sales analytics", True),  # case-insensitive
        ("*-prod", "Finance-PROD", True),
        ("*-prod", "Finance-PROD-copy", False),
        ("S?les", "Sales", True),
        ("S?les", "Sles", False),
        ("*", "", True),
        ("", "", True),
        ("a*b*c", "a--b--c", True),
        ("a*b*c", "a--b--d", False),
        ("*a", "banana", True),
        ("*a", "bananas", False),
        # `[PROD]` is literal, not a character class: this is the naming convention people actually use.
        ("Sales [PROD]", "Sales [PROD]", True),
        ("Sales [PROD]", "Sales P", False),
        ("*[PROD]", "Sales [PROD]", True),
        ("*[PROD]", "Sales D", False),
        # Regex metacharacters are literal.
        ("Sales.+", "Sales.+", True),
        ("Sales.+", "Salesxx", False),
    ],
)
def test_name_matching(pattern: str, name: str, expected: bool) -> None:
    assert name_matches(pattern, name) is expected


def test_a_hostile_pattern_cannot_cause_backtracking_blowup() -> None:
    pattern = "a*" * 60 + "b"
    started = time.perf_counter()
    assert name_matches(pattern, "a" * 4000) is False
    assert time.perf_counter() - started < 5.0
