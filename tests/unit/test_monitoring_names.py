"""Which KQL database is a workspace's monitoring database (documented names; nothing is observed live yet)."""

from __future__ import annotations

import pytest

from orqentis.fabric.monitoring_names import LEGACY_DATABASE_NAME, is_monitoring_database


@pytest.mark.parametrize(
    "name",
    [LEGACY_DATABASE_NAME, "monitoring kql DATABASE", "  Monitoring KQL database "],
)
def test_the_legacy_database_matches_in_every_workspace(name: str) -> None:
    assert is_monitoring_database(name, workspace_name="Anything", override="")
    assert is_monitoring_database(name, workspace_name=None, override="")


@pytest.mark.parametrize(
    ("workspace", "database"),
    [
        ("Sales", "Sales Monitoring_Database"),
        ("Sales [PROD]", "Sales _PROD_ Monitoring_Database"),
        ("Sales [PROD]", "sales_prod_monitoring_database"),
        ("Finance & Risk (TEST)", "Finance _ Risk _TEST_ Monitoring_Database"),
        ("Café", "Café Monitoring_Database"),
        ("Sales", "Sales_Database"),  # a monitoring item with an empty-looking name
    ],
)
def test_the_monitoring_item_s_database_matches_the_workspace_it_is_named_for(workspace: str, database: str) -> None:
    assert is_monitoring_database(database, workspace_name=workspace, override="")


@pytest.mark.parametrize(
    ("workspace", "database"),
    [
        ("Sales", "Finance Monitoring_Database"),  # another workspace's
        ("Sales", "Sales events"),  # an ordinary KQL database
        ("Sales", "Sales Monitoring"),  # no "_Database" suffix
        ("Sales", "Salesforce_Database"),  # starts with the workspace name but is not "<workspace> <item>"
        ("Sales", "Database"),
        ("", "Monitoring_Database"),  # a workspace with no usable name cannot be matched by the second rule
        ("!!!", "_Database"),
    ],
)
def test_other_databases_do_not_match(workspace: str, database: str) -> None:
    assert not is_monitoring_database(database, workspace_name=workspace, override="")


def test_without_a_workspace_name_only_the_legacy_name_can_match() -> None:
    assert not is_monitoring_database("Sales Monitoring_Database", workspace_name=None, override="")


def test_a_configured_name_replaces_the_rules() -> None:
    assert is_monitoring_database("Ops monitoring", workspace_name="Sales", override=" ops MONITORING ")
    assert not is_monitoring_database(LEGACY_DATABASE_NAME, workspace_name="Sales", override="Ops monitoring")
    assert not is_monitoring_database("Sales Monitoring_Database", workspace_name="Sales", override="Ops monitoring")
