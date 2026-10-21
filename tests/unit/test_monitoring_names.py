"""Which KQL database is a workspace's monitoring database (documented names; nothing is observed live yet)."""

from __future__ import annotations

import pytest

from orqentis.fabric.monitoring_names import (
    LEGACY_DATABASE_NAME,
    Identification,
    identify_monitoring_database,
    is_monitoring_database,
)


@pytest.mark.parametrize(
    "name",
    [LEGACY_DATABASE_NAME, "monitoring kql DATABASE", "  Monitoring KQL database "],
)
def test_the_legacy_database_matches_in_every_workspace_and_is_named(name: str) -> None:
    assert identify_monitoring_database(name, workspace_name="Anything") is Identification.NAMED
    assert identify_monitoring_database(name, workspace_name=None) is Identification.NAMED


@pytest.mark.parametrize(
    ("workspace", "database"),
    [
        ("Sales", "Sales Monitoring_Database"),
        ("Sales", "Sales_Monitoring_Database"),
        ("Sales [PROD]", "Sales _PROD_ Monitoring_Database"),
        ("Sales [PROD]", "Sales [PROD] Monitoring_Database"),  # a character Fabric did not have to replace
        ("Finance & Risk (TEST)", "Finance _ Risk _TEST_ Monitoring_Database"),
        ("Café", "Café Monitoring_Database"),
        ("Sales", "Sales Monitoring 2_Database"),
        (
            "Sales",
            "Sales_Archive_Database",
        ),  # indistinguishable from the above by name: that is why it is only INFERRED
    ],
)
def test_the_monitoring_item_s_database_matches_the_workspace_it_is_named_for_but_is_only_inferred(
    workspace: str, database: str
) -> None:
    assert identify_monitoring_database(database, workspace_name=workspace) is Identification.INFERRED


@pytest.mark.parametrize(
    ("workspace", "database"),
    [
        ("Sales", "Sales Database"),  # the probe: a space, not the literal "_Database"
        ("Sales", "Sales-Archive-Database"),
        ("Sales", "Sales Archive database"),
        ("Sales", "Sales Monitoring_database"),  # the suffix is matched as Fabric writes it
        ("Sales", "sales Monitoring_Database"),  # and so is the workspace name
        ("Sales", "Sales_Database"),  # no item name
        ("Sales", "Sales__Database"),  # a separator is not an item name
        ("Sales", "Salesforce_Database"),  # starts with the workspace name but has no separator after it
        ("Sales", "Finance Monitoring_Database"),  # another workspace's
        ("Sales", "Sales events"),  # an ordinary KQL database
        ("Sales", "Sales Monitoring"),
        ("Sales", "Database"),
        ("Sales [PROD]", "Sales -PROD- Monitoring_Database"),  # "-" is not what Fabric writes for a replaced character
        ("", "Monitoring_Database"),  # a workspace with no name cannot be matched by the second rule
    ],
)
def test_other_databases_do_not_match(workspace: str, database: str) -> None:
    assert identify_monitoring_database(database, workspace_name=workspace) is None
    assert not is_monitoring_database(database, workspace_name=workspace)


def test_without_a_workspace_name_only_the_legacy_name_can_match() -> None:
    assert identify_monitoring_database("Sales Monitoring_Database", workspace_name=None) is None


def test_a_configured_name_replaces_the_rules_and_is_named() -> None:
    assert identify_monitoring_database("Ops monitoring", workspace_name="Sales", override=" ops MONITORING ") is (
        Identification.NAMED
    )
    assert not is_monitoring_database(LEGACY_DATABASE_NAME, workspace_name="Sales", override="Ops monitoring")
    assert not is_monitoring_database("Sales Monitoring_Database", workspace_name="Sales", override="Ops monitoring")
