"""Settings of the WS7 signal sources: defaults per mode, and the ways a typo must not pass silently."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from orqentis.config.settings import COLLECTOR_NAMES, FabricMode, RefreshSource
from tests.conftest import make_settings

GUID_A = "11111111-1111-4111-8111-111111111111"
GUID_B = "22222222-2222-4222-8222-222222222222"


def test_mock_mode_replays_a_power_bi_history_and_rest_mode_reads_workspace_monitoring(tmp_path: Path) -> None:
    assert make_settings(tmp_path).refresh_source is RefreshSource.POWERBI_API
    rest = make_settings(tmp_path, fabric_mode=FabricMode.REST, tenant_id="t")
    assert rest.refresh_source is RefreshSource.WORKSPACE_MONITORING
    forced = make_settings(tmp_path, fabric_mode=FabricMode.REST, tenant_id="t", refresh_source=RefreshSource.BOTH)
    assert forced.refresh_source is RefreshSource.BOTH  # an explicit choice is kept


def test_the_sla_telemetry_check_is_on_in_rest_mode_and_off_in_mock_mode(tmp_path: Path) -> None:
    assert make_settings(tmp_path).sla_max_telemetry_lag_minutes == 0
    assert make_settings(tmp_path, fabric_mode=FabricMode.REST, tenant_id="t").sla_max_telemetry_lag_minutes == 30
    assert (
        make_settings(
            tmp_path, fabric_mode=FabricMode.REST, tenant_id="t", sla_max_telemetry_lag_minutes=0
        ).sla_max_telemetry_lag_minutes
        == 0
    )


def test_the_capacity_metrics_app_is_both_ids_or_neither(tmp_path: Path) -> None:
    both = make_settings(tmp_path, capacity_metrics_workspace_id=GUID_A, capacity_metrics_dataset_id=GUID_B)
    assert both.capacity_metrics_dataset_id == GUID_B
    for only in ({"capacity_metrics_workspace_id": GUID_A}, {"capacity_metrics_dataset_id": GUID_B}):
        with pytest.raises(ValidationError, match="go together"):
            make_settings(tmp_path, **only)
    with pytest.raises(ValidationError):
        make_settings(tmp_path, capacity_metrics_workspace_id="not a guid", capacity_metrics_dataset_id=GUID_B)


def test_expected_forbidden_collectors_must_be_real_collectors(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, expected_forbidden_collectors=" semantic-model-refreshes , capacity-metrics,semantic-model-refreshes"
    )
    assert settings.expected_forbidden_collector_names == ("semantic-model-refreshes", "capacity-metrics")
    assert make_settings(tmp_path).expected_forbidden_collector_names == ()
    with pytest.raises(ValidationError, match="unknown collector"):
        make_settings(
            tmp_path, expected_forbidden_collectors="semantic-model-refresh"
        )  # a typo would silently do nothing
    assert "semantic-model-refreshes" in COLLECTOR_NAMES and "delta-schema-changes" in COLLECTOR_NAMES


@pytest.mark.parametrize("field", ["kusto_scope", "onelake_scope"])
def test_scopes_must_be_a_resource_ending_in_default(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, **{field: "https://example.invalid/user_impersonation"})


@pytest.mark.parametrize(
    "url", ["http://onelake.example.invalid", "https://onelake.example.invalid/some/path", "onelake", "https://x?y=1"]
)
def test_the_onelake_endpoint_is_an_https_origin(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValidationError, match="https origin"):
        make_settings(tmp_path, onelake_base_url=url)
    assert make_settings(tmp_path, onelake_base_url="https://onelake.dfs.fabric.microsoft.us/").onelake_base_url == (
        "https://onelake.dfs.fabric.microsoft.us"
    )


def test_the_delta_log_read_is_bounded(tmp_path: Path) -> None:
    for bad in ({"delta_log_max_commit_bytes": 10}, {"delta_log_max_commits": 0}, {"delta_log_max_commits": 5000}):
        with pytest.raises(ValidationError):
            make_settings(tmp_path, **bad)
