"""Deployed environments read the contracts file from a blob: load, reload without a restart, and fail safely.

The application runs in REST mode on PostgreSQL + Azurite (Fabric is mocked at the HTTP transport).
``contracts_path`` is not set, so the contracts come from ``<contracts_container>/estate.json`` in the evidence
account -- exactly as in a Container App, where a person uploads the reviewed file with
``az storage blob upload --auth-mode login``.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from azure.storage.blob.aio import BlobServiceClient

from orqentis.api.app import create_app
from orqentis.bootstrap import Container, build_container
from orqentis.config.clock import FixedClock
from orqentis.config.settings import FabricMode, Settings
from orqentis.contracts.errors import ContractsFileNotFoundError, ContractsInvalidError, ContractsNotConfiguredError
from tests.conftest import REPO
from tests.support.polling import wait_until
from tests.support.postgres_app import AppStorage
from tests.support.rest_estate import EXTRA_DEPENDENCY, NOW, contracts_bytes, mock_fabric

BLOB = "estate.json"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Uploader:
    """The person with Storage Blob Data Contributor on the contracts container (the app only has Reader)."""

    def __init__(self, service: BlobServiceClient, container: str) -> None:
        self._container = service.get_container_client(container)

    async def upload(self, data: bytes) -> None:
        await self._container.upload_blob(BLOB, data, overwrite=True)  # az storage blob upload --overwrite

    async def delete(self) -> None:
        await self._container.delete_blob(BLOB)


@pytest.fixture
def uploader(blob_service: BlobServiceClient, app_storage: AppStorage) -> Uploader:
    return Uploader(blob_service, app_storage.contracts_container)


@pytest.fixture(autouse=True)
def fabric(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_fabric(monkeypatch)


def settings_for(app_storage: AppStorage, tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "fabric_mode": FabricMode.REST,
        "tenant_id": "tenant-test",
        "runbooks_dir": REPO / "runbooks",
        "fixtures_dir": REPO / "fixtures",
        "startup_grace_seconds": 0,
        "build_retry_initial_seconds": 0.01,
        "build_retry_max_seconds": 0.05,
    }
    values.update(overrides)
    return app_storage.settings(tmp_path, **values)


@pytest.fixture
async def container(app_storage: AppStorage, uploader: Uploader, tmp_path: Path) -> AsyncIterator[Container]:
    await uploader.upload(contracts_bytes())
    c = await build_container(settings_for(app_storage, tmp_path / "a"), clock=FixedClock(NOW))
    try:
        yield c
    finally:
        await c.investigation_jobs.shutdown()
        await c.aclose()


async def loaded_records(container: Container) -> list[dict[str, object]]:
    return [dict(r.details) for r in await container.audit.list() if r.action == "contracts.loaded"]


def edge_ids(container: Container) -> set[str]:
    return {e.id for e in container.graph.edges}


@pytest.mark.security
async def test_the_app_loads_the_contracts_from_the_blob_and_audits_which_version_and_sha256(
    container: Container, app_storage: AppStorage
) -> None:
    assert container.settings.contracts_path is None
    assert edge_ids(container) == {"d1", "d2", "d3"}
    (record,) = await loaded_records(container)
    assert record["contracts_sha256"] == sha(contracts_bytes())
    assert record["contracts_path"] == f"blob:{app_storage.contracts_container}/{BLOB}"
    assert record["contracts_version"]  # the blob version (or ETag) that was read


async def test_a_new_upload_is_picked_up_by_the_next_refresh_without_a_restart(
    container: Container, uploader: Uploader
) -> None:
    changed = contracts_bytes(EXTRA_DEPENDENCY)
    await uploader.upload(changed)

    diff = await container.catalog_refresher.refresh()

    assert diff.dependencies.added == ["d4"] and edge_ids(container) == {"d1", "d2", "d3", "d4"}
    first, second = await loaded_records(container)
    assert second["contracts_sha256"] == sha(changed) and first["contracts_sha256"] != second["contracts_sha256"]
    assert second["contracts_version"] and second["contracts_version"] != first["contracts_version"]
    assert (await container.audit.verify()).valid
    assert (await container.catalog_refresher.refresh()).is_empty  # the same blob again: no-op
    assert len(await loaded_records(container)) == 2


async def test_a_bad_upload_keeps_the_last_good_contracts_and_the_next_good_one_recovers(
    container: Container, uploader: Uploader
) -> None:
    await uploader.upload(b'{"dependencies": [], "client_secret": "hunter2-SECRET"')
    with pytest.raises(ContractsInvalidError) as invalid:
        await container.catalog_refresher.refresh()
    assert "hunter2" not in str(invalid.value) and f"/{BLOB}" in str(invalid.value)
    assert edge_ids(container) == {"d1", "d2", "d3"} and len(await loaded_records(container)) == 1

    await uploader.delete()
    with pytest.raises(ContractsFileNotFoundError):
        await container.catalog_refresher.refresh()
    assert edge_ids(container) == {"d1", "d2", "d3"}

    await uploader.upload(contracts_bytes(EXTRA_DEPENDENCY))
    assert (await container.catalog_refresher.refresh()).dependencies.added == ["d4"]
    assert len(await loaded_records(container)) == 2


async def test_every_replica_reloads_its_own_memory_so_a_follower_does_not_keep_the_old_contracts(
    container: Container, app_storage: AppStorage, uploader: Uploader, tmp_path: Path
) -> None:
    follower = await build_container(settings_for(app_storage, tmp_path / "b"), clock=FixedClock(NOW))
    try:
        assert edge_ids(follower) == {"d1", "d2", "d3"}
        await uploader.upload(contracts_bytes(EXTRA_DEPENDENCY))

        # The leader's scheduled job: all the effects, once.
        await container.catalog_refresher.refresh()
        # The other replica's own loop: its memory only.
        assert await follower.replica_sync.run_once()

        assert edge_ids(container) == edge_ids(follower) == {"d1", "d2", "d3", "d4"}
        actions = [r.action for r in await container.audit.list()]
        assert actions.count("catalog.refresh") == 1  # audited once, not once per replica
        # Each replica says which contracts version it runs: two start-up loads and two reloads (shared audit trail).
        assert actions.count("contracts.loaded") == 4
        assert [r["contracts_sha256"] for r in (await loaded_records(follower))[-2:]] == [
            sha(contracts_bytes(EXTRA_DEPENDENCY))
        ] * 2
    finally:
        await follower.investigation_jobs.shutdown()
        await follower.aclose()


@pytest.mark.security
async def test_a_contracts_path_wins_over_the_blob_so_a_local_file_cannot_be_shadowed_by_a_stale_upload(
    app_storage: AppStorage, uploader: Uploader, tmp_path: Path
) -> None:
    await uploader.upload(contracts_bytes(EXTRA_DEPENDENCY))  # the blob says d4 exists...
    local = tmp_path / "local.json"
    local.write_bytes(contracts_bytes())  # ...the explicitly configured file does not
    c = await build_container(settings_for(app_storage, tmp_path / "a", contracts_path=local), clock=FixedClock(NOW))
    try:
        assert edge_ids(c) == {"d1", "d2", "d3"}
        (record,) = await loaded_records(c)
        assert record["contracts_path"] == str(local)
    finally:
        await c.investigation_jobs.shutdown()
        await c.aclose()


async def test_rest_mode_without_any_contracts_source_is_a_typed_configuration_error(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        fabric_mode=FabricMode.REST,
        tenant_id="tenant-test",
        data_dir=tmp_path / "var",
        runbooks_dir=REPO / "runbooks",
        fixtures_dir=REPO / "fixtures",
    )
    with pytest.raises(ContractsNotConfiguredError):
        await build_container(settings, reset=True)


def client_for(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")  # type: ignore[arg-type]


@pytest.mark.security
async def test_a_missing_blob_keeps_the_app_alive_and_not_ready_with_the_typed_error_then_it_recovers(
    app_storage: AppStorage, uploader: Uploader, tmp_path: Path
) -> None:
    app = create_app(settings_for(app_storage, tmp_path / "a"))
    async with app.router.lifespan_context(app), client_for(app) as http:
        await wait_until(lambda: app.state.runtime.status.attempts >= 2)  # retried with backoff, not given up on
        assert app.state.runtime.status.error_type == "ContractsFileNotFoundError"
        # Liveness is unaffected: no crash loop...
        assert (await http.get("/health")).status_code == 200
        # ...readiness names the typed error and nothing else (not the blob's address, not its content)...
        not_ready = await http.get("/health/ready")
        assert not_ready.status_code == 503
        body = not_ready.json()
        assert body["status"] == "not_ready" and body["reason"] == "build_failed"
        assert body["error_type"] == "ContractsFileNotFoundError"
        assert app_storage.contracts_container not in not_ready.text and BLOB not in not_ready.text
        assert (await http.get("/incidents", headers={"X-Orqentis-User": "v@contoso.com"})).status_code == 503

        # ...and the upload that fixes it needs no restart.
        await uploader.upload(contracts_bytes())
        await wait_until(lambda: app.state.runtime.ready)
        assert (await http.get("/health/ready")).status_code == 200
        container = app.state.runtime.container
        assert container is not None and edge_ids(container) == {"d1", "d2", "d3"}


async def test_an_invalid_blob_at_start_up_is_not_ready_with_the_typed_error_and_is_never_echoed(
    app_storage: AppStorage, uploader: Uploader, tmp_path: Path
) -> None:
    await uploader.upload(b'{"dependencies": [], "client_secret": "hunter2-SECRET"}')
    app = create_app(settings_for(app_storage, tmp_path / "a"))
    async with app.router.lifespan_context(app), client_for(app) as http:
        await wait_until(lambda: app.state.runtime.status.error_type is not None)
        not_ready = await http.get("/health/ready")
        assert not_ready.status_code == 503 and not_ready.json()["error_type"] == "ContractsInvalidError"
        assert "hunter2" not in not_ready.text
        assert (await http.get("/health")).status_code == 200

        await uploader.upload(contracts_bytes())
        await wait_until(lambda: app.state.runtime.ready)
