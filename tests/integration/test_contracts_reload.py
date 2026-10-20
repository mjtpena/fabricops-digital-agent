"""A new version of the contracts file reaches a running app without a restart; a bad one never replaces a good one.

REST mode on a mocked Fabric transport, contracts from a file (the blob variants of these tests are in
``test_contracts_blob_app.py``, which needs Azurite). The catalog refresh is what re-reads the file.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from orqentis.api.app import create_app
from orqentis.bootstrap import Container, build_container
from orqentis.config.clock import FixedClock
from orqentis.config.settings import FabricMode
from orqentis.contracts.errors import ContractsFileNotFoundError, ContractsInvalidError
from tests.conftest import REPO, make_settings
from tests.support.rest_estate import EXTRA_DEPENDENCY, NOW, contracts_bytes, mock_fabric


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def publish(path: Path, data: bytes) -> None:
    """What a person does to the file: replace its content."""
    path.write_bytes(data)


def withdraw(path: Path) -> None:
    path.unlink()


@pytest.fixture
def contracts_file(tmp_path: Path) -> Path:
    path = tmp_path / "estate.json"
    path.write_bytes(contracts_bytes())
    return path


@pytest.fixture
async def container(contracts_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Container]:
    mock_fabric(monkeypatch)
    settings = make_settings(
        tmp_path,
        fabric_mode=FabricMode.REST,
        tenant_id="tenant-test",
        contracts_path=contracts_file,
        runbooks_dir=REPO / "runbooks",
        fixtures_dir=REPO / "fixtures",
    )
    c = await build_container(settings, reset=True, clock=FixedClock(NOW))
    try:
        yield c
    finally:
        await c.investigation_jobs.shutdown()
        await c.aclose()


async def loaded_records(container: Container) -> list[dict[str, object]]:
    return [dict(r.details) for r in await container.audit.list() if r.action == "contracts.loaded"]


def edge_ids(container: Container) -> set[str]:
    return {e.id for e in container.graph.edges}


async def test_the_sha256_of_the_contracts_in_force_is_audited_when_the_app_starts(
    container: Container, contracts_file: Path
) -> None:
    records = await loaded_records(container)
    assert len(records) == 1
    assert records[0]["contracts_sha256"] == sha(contracts_bytes())
    assert records[0]["contracts_path"] == str(contracts_file)
    assert edge_ids(container) == {"d1", "d2", "d3"}


async def test_a_changed_file_is_validated_applied_and_audited_by_the_next_refresh_without_a_restart(
    container: Container, contracts_file: Path
) -> None:
    changed = contracts_bytes(EXTRA_DEPENDENCY)
    publish(contracts_file, changed)

    diff = await container.catalog_refresher.refresh()

    assert diff.dependencies.added == ["d4"]
    assert edge_ids(container) == {"d1", "d2", "d3", "d4"}
    assert {e.id for e in await container.fabric.dependencies.list_dependencies()} == {"d1", "d2", "d3", "d4"}
    records = await loaded_records(container)
    assert [r["contracts_sha256"] for r in records][-1] == sha(changed)
    assert len(records) == 2 and records[0]["contracts_sha256"] != records[1]["contracts_sha256"]
    assert [r.action for r in await container.audit.list()].count("catalog.refresh") == 1
    assert (await container.audit.verify()).valid


async def test_the_same_bytes_are_a_no_op_but_new_bytes_with_the_same_meaning_are_still_audited(
    container: Container, contracts_file: Path
) -> None:
    assert (await container.catalog_refresher.refresh()).is_empty
    assert len(await loaded_records(container)) == 1  # nothing changed: nothing recorded

    reformatted = contracts_bytes(layout="indented")  # same contracts, different bytes: a different file version
    publish(contracts_file, reformatted)
    assert (await container.catalog_refresher.refresh()).is_empty  # no estate change...
    records = await loaded_records(container)
    assert len(records) == 2 and records[-1]["contracts_sha256"] == sha(reformatted)  # ...but the version is audited
    assert [r.action for r in await container.audit.list()].count("catalog.refresh") == 0


async def test_an_invalid_file_keeps_the_contracts_that_were_in_force_and_a_fixed_one_recovers(
    container: Container, contracts_file: Path
) -> None:
    before = edge_ids(container)
    publish(contracts_file, b'{"dependencies": [], "client_secret": "hunter2-SECRET"')
    with pytest.raises(ContractsInvalidError) as excinfo:
        await container.catalog_refresher.refresh()
    assert "hunter2" not in str(excinfo.value)
    assert edge_ids(container) == before and len(await loaded_records(container)) == 1

    withdraw(contracts_file)
    with pytest.raises(ContractsFileNotFoundError):
        await container.catalog_refresher.refresh()
    assert edge_ids(container) == before and len(await loaded_records(container)) == 1

    fixed = contracts_bytes(EXTRA_DEPENDENCY)
    publish(contracts_file, fixed)
    diff = await container.catalog_refresher.refresh()
    assert diff.dependencies.added == ["d4"] and edge_ids(container) == before | {"d4"}
    assert (await loaded_records(container))[-1]["contracts_sha256"] == sha(fixed)


async def test_the_scheduled_refresh_job_reports_a_bad_file_as_a_failure_and_keeps_serving(
    container: Container, contracts_file: Path
) -> None:
    from datetime import timedelta

    clock = container.clock
    assert isinstance(clock, FixedClock)
    await container.scheduler.tick()  # minute 0: catalog-refresh waits one interval before its first run
    await container.scheduler.wait_idle()
    publish(contracts_file, b"not json")
    clock.advance(timedelta(minutes=container.settings.scheduler_catalog_refresh_interval_minutes))
    assert "catalog-refresh" in await container.scheduler.tick()
    await container.scheduler.wait_idle()

    job = next(j for j in container.scheduler.status().jobs if j.name == "catalog-refresh")
    assert job.failure_count == 1 and job.last_outcome == "failed"
    assert job.last_error is not None and "ContractsInvalidError" in job.last_error
    assert edge_ids(container) == {"d1", "d2", "d3"}  # still on the last good contracts


async def test_a_follower_applies_new_contracts_to_its_own_memory_and_leaves_the_shared_effects_to_the_leader(
    container: Container, contracts_file: Path
) -> None:
    assert not container.scheduler.is_leader  # no scheduler running here: this replica is a follower
    publish(contracts_file, contracts_bytes(EXTRA_DEPENDENCY))

    assert await container.replica_sync.run_once()

    assert edge_ids(container) == {"d1", "d2", "d3", "d4"}  # this replica now evaluates against the new contracts
    actions = [r.action for r in await container.audit.list()]
    assert actions.count("contracts.loaded") == 2  # every replica says which contracts version it runs
    assert "catalog.refresh" not in actions  # the diff is audited once, by the leader's scheduled job


def client_for(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")  # type: ignore[arg-type]


async def failed_records(container: Container) -> list[dict[str, object]]:
    return [dict(r.details) for r in await container.audit.list() if r.action == "contracts.reload"]


@pytest.mark.security
async def test_a_refused_reload_is_logged_audited_once_per_refused_digest_and_makes_the_app_degraded_not_unready(
    container: Container, contracts_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(container.settings, container=container)
    async with client_for(app) as http:
        healthy = await http.get("/health/ready")
        assert healthy.status_code == 200 and healthy.json() == {"status": "ready", "scheduler": "disabled"}

        bad = b'{"dependencies": [], "client_secret": "hunter2-SECRET"'
        publish(contracts_file, bad)
        with caplog.at_level(logging.ERROR), pytest.raises(ContractsInvalidError):
            await container.catalog_refresher.refresh()
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
            "contracts reload failed; the last good contracts stay in force"
        ]
        (record,) = await failed_records(container)
        assert record["rejected_sha256"] == sha(bad) and record["error_type"] == "ContractsInvalidError"
        assert record["contracts_sha256_in_force"] == sha(contracts_bytes())  # the last good ones are still in force
        assert "hunter2" not in json.dumps(record)  # structure only, never content

        # Still ready (the service keeps monitoring on the last good contracts), but it says what is wrong.
        degraded = await http.get("/health/ready")
        assert degraded.status_code == 200
        assert degraded.json() == {"status": "ready", "scheduler": "disabled", "degraded": ["contracts reload failed"]}

        # The same refused file again is the same news: not another audit record on every interval.
        with pytest.raises(ContractsInvalidError):
            await container.catalog_refresher.refresh()
        assert len(await failed_records(container)) == 1
        # A different refused file is new information.
        publish(contracts_file, b"not json either")
        with pytest.raises(ContractsInvalidError):
            await container.catalog_refresher.refresh()
        assert [r["rejected_sha256"] for r in await failed_records(container)] == [sha(bad), sha(b"not json either")]
        # A missing file is reported too (no digest, since nothing could be read).
        withdraw(contracts_file)
        with pytest.raises(ContractsFileNotFoundError):
            await container.catalog_refresher.refresh()
        assert (await failed_records(container))[-1]["rejected_sha256"] is None

        # A good upload clears the degraded state.
        publish(contracts_file, contracts_bytes(EXTRA_DEPENDENCY))
        await container.catalog_refresher.refresh()
        recovered = await http.get("/health/ready")
        assert recovered.json() == {"status": "ready", "scheduler": "disabled"}
        assert (await container.audit.verify()).valid


async def test_a_follower_promoted_after_applying_a_change_still_repairs_the_shared_dependency_table(
    container: Container, contracts_file: Path
) -> None:
    """A follower applies a change to its memory only; if it then becomes the leader its refresh sees an empty diff."""
    store = container.catalog_refresher._store  # type: ignore[attr-defined]
    assert {e.id for e in await store.list_edges()} == {"d1", "d2", "d3"}
    publish(contracts_file, contracts_bytes(EXTRA_DEPENDENCY))
    assert await container.replica_sync.run_once()  # follower: memory only
    assert edge_ids(container) == {"d1", "d2", "d3", "d4"}
    assert {e.id for e in await store.list_edges()} == {"d1", "d2", "d3"}  # the shared table is behind

    diff = await container.catalog_refresher.refresh()  # now this replica leads: its own diff is empty...
    assert diff.is_empty
    assert {e.id for e in await store.list_edges()} == {"d1", "d2", "d3", "d4"}  # ...and it repaired the table
