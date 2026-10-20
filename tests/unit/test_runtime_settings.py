"""Settings for the autonomous runtime, and the interrupted-investigation evidence guarantee."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from orqentis.config.clock import FixedClock
from orqentis.config.settings import ApiAuthMode, Settings
from orqentis.domain.evidence import EvidenceType
from orqentis.domain.incidents import Incident
from orqentis.domain.taxonomy import IncidentCategory, Severity
from orqentis.evidence.recorder import EvidenceRecorder
from orqentis.storage.evidence_store import LocalEvidenceStore

REPO = Path(__file__).resolve().parents[2]


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_the_scheduler_is_off_locally_and_on_when_deployed() -> None:
    assert settings().scheduler_enabled is False
    for environment in ("dev", "test", "prod"):
        deployed = settings(deployment_environment=environment, api_auth_mode=ApiAuthMode.EASY_AUTH)
        assert deployed.scheduler_enabled is True, environment


def test_the_scheduler_waits_before_starting_when_deployed_so_a_draining_revision_can_finish() -> None:
    assert settings().scheduler_startup_delay_seconds == 0  # local and tests start at once
    for environment in ("dev", "test", "prod"):
        deployed = settings(deployment_environment=environment, api_auth_mode=ApiAuthMode.EASY_AUTH)
        assert deployed.scheduler_startup_delay_seconds == 120, environment
    explicit = settings(
        deployment_environment="prod", api_auth_mode=ApiAuthMode.EASY_AUTH, scheduler_startup_delay_seconds=0
    )
    assert explicit.scheduler_startup_delay_seconds == 0
    assert settings(scheduler_startup_delay_seconds=30).scheduler_startup_delay_seconds == 30


def test_an_explicit_choice_always_wins() -> None:
    assert settings(scheduler_enabled=True).scheduler_enabled is True
    off = settings(deployment_environment="prod", api_auth_mode=ApiAuthMode.EASY_AUTH, scheduler_enabled=False)
    assert off.scheduler_enabled is False


def test_the_environment_variable_form_is_understood(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORQENTIS_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("ORQENTIS_SCHEDULER_COLLECT_INTERVAL_MINUTES", "2")
    monkeypatch.setenv("ORQENTIS_FABRIC_MAX_CONCURRENCY", "8")
    monkeypatch.setenv("ORQENTIS_COLLECTION_OVERLAP_MINUTES", "30")
    configured = Settings(_env_file=None)  # type: ignore[call-arg]
    assert configured.scheduler_enabled and configured.scheduler_collect_interval_minutes == 2
    assert configured.fabric_max_concurrency == 8 and configured.collection_overlap_minutes == 30


def test_defaults_are_conservative() -> None:
    s = settings()
    assert s.scheduler_collect_interval_minutes == 5
    assert (s.fabric_max_concurrency, s.fabric_rate_per_second, s.fabric_rate_burst) == (4, 5.0, 10)
    assert s.collection_max_lookback_hours == 168 and s.collection_overlap_minutes == 15
    assert s.investigation_max_concurrency == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"scheduler_collect_interval_minutes": 0},
        {"fabric_max_concurrency": 0},
        {"fabric_rate_per_second": 0},
        {"collection_max_lookback_hours": 0},
        {"investigation_timeout_seconds": 5},
        {"build_retry_initial_seconds": 10, "build_retry_max_seconds": 5},
        {"scheduler_tick_seconds": 30, "scheduler_lease_ttl_seconds": 40},  # the lease would lapse between ticks
        {"scheduler_tick_seconds": 20, "scheduler_lease_ttl_seconds": 59},  # needs at least three ticks per TTL
        {"scheduler_startup_delay_seconds": -1},
    ],
)
def test_nonsensical_values_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic's ValidationError is a ValueError
        settings(**bad)


def test_every_setting_is_documented_in_env_example() -> None:
    documented = (REPO / ".env.example").read_text(encoding="utf-8")
    missing = [
        f"ORQENTIS_{name.upper()}"
        for name in Settings.model_fields
        if name != "applicationinsights_connection_string" and f"ORQENTIS_{name.upper()}" not in documented
    ]
    assert missing == [], f"add these to .env.example: {missing}"


def incident() -> Incident:
    now = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    return Incident(
        incident_id="INC-2026-0001",
        tenant_id="t",
        title="x",
        severity=Severity.HIGH,
        category=IncidentCategory.PIPELINE_FAILURE,
        detected_at=now,
        source="test",
        workspace_id="ws",
        primary_item_id="item",
        created_at=now,
        updated_at=now,
    )


async def test_a_retry_after_an_interrupted_investigation_never_collides_with_its_leftover_artefacts(
    tmp_path: Path,
) -> None:
    store = LocalEvidenceStore(tmp_path / "evidence")
    clock = FixedClock(datetime(2026, 10, 20, 8, 0, tzinfo=UTC))

    # First attempt: records two pieces of evidence, then dies; the incident forgets them, the raw files stay.
    first = EvidenceRecorder(incident(), store, clock)
    leftovers = [
        await first.record(evidence_type=EvidenceType.ERROR_MESSAGE, summary="a", source="t", raw={"n": 1}),
        await first.record(evidence_type=EvidenceType.ERROR_MESSAGE, summary="b", source="t", raw={"n": 2}),
    ]
    assert [e.evidence_id for e in leftovers] == ["INC-2026-0001-EV001", "INC-2026-0001-EV002"]
    original = await store.get_text(leftovers[0].raw_reference or "")

    # Second attempt starts from the stored incident, which has no evidence: it must not fail or overwrite.
    retry = EvidenceRecorder(incident(), store, clock)
    fresh = await retry.record(evidence_type=EvidenceType.ERROR_MESSAGE, summary="c", source="t", raw={"n": 3})
    assert fresh.evidence_id == "INC-2026-0001-EV003"
    assert await store.get_text(leftovers[0].raw_reference or "") == original  # raw artefacts stay immutable
