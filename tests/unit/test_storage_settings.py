"""Storage settings: what a deployed environment must configure, and what only a local test container may."""

from __future__ import annotations

from pathlib import Path

import pytest

from orqentis.bootstrap import build_container
from orqentis.config.settings import ApiAuthMode, PostgresAuth, PostgresSslMode, Settings, StorageBackend
from tests.conftest import make_settings

POSTGRES = {
    "storage_backend": StorageBackend.POSTGRES,
    "postgres_host": "psql-x.postgres.database.azure.com",
    "postgres_user": "id-fabricops-dev-reader",
    "evidence_blob_url": "https://stfabricops.blob.core.windows.net",
}


def deployed(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {"deployment_environment": "dev", "api_auth_mode": ApiAuthMode.EASY_AUTH, **POSTGRES}
    values.update(overrides)
    return make_settings(tmp_path, **values)


def test_local_storage_is_the_default_and_needs_nothing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert settings.storage_backend is StorageBackend.LOCAL
    settings.check_storage_for_environment()


def test_the_postgres_backend_needs_a_host_a_role_and_a_blob_endpoint(tmp_path: Path) -> None:
    for missing in ("postgres_host", "postgres_user", "evidence_blob_url"):
        values = {k: v for k, v in POSTGRES.items() if k != missing}
        with pytest.raises(ValueError, match=f"ORQENTIS_{missing.upper()}"):
            make_settings(tmp_path, **values)
    make_settings(tmp_path, **POSTGRES)


@pytest.mark.parametrize("environment", ["dev", "test", "prod"])
def test_deployed_environments_refuse_local_storage(tmp_path: Path, environment: str) -> None:
    settings = make_settings(tmp_path, deployment_environment=environment, api_auth_mode=ApiAuthMode.EASY_AUTH)
    with pytest.raises(ValueError, match="requires ORQENTIS_STORAGE_BACKEND=postgres"):
        settings.check_storage_for_environment()
    deployed(tmp_path, deployment_environment=environment).check_storage_for_environment()


@pytest.mark.security
def test_passwordless_and_unencrypted_database_access_is_for_a_local_test_container_only(tmp_path: Path) -> None:
    make_settings(tmp_path, **POSTGRES, postgres_auth=PostgresAuth.NONE, postgres_ssl_mode=PostgresSslMode.DISABLE)
    for overrides in ({"postgres_auth": PostgresAuth.NONE}, {"postgres_ssl_mode": PostgresSslMode.DISABLE}):
        with pytest.raises(ValueError, match="only permitted when deployment_environment=local"):
            deployed(tmp_path, **overrides)
    with pytest.raises(ValueError, match="postgres_ssl_mode"):
        deployed(tmp_path, postgres_ssl_mode="prefer")


@pytest.mark.security
def test_there_is_no_way_to_configure_a_database_password() -> None:
    names = {name for name in Settings.model_fields if "postgres" in name or "evidence" in name or "anchor" in name}
    assert names
    assert not [n for n in names if any(word in n for word in ("password", "secret", "key", "token", "sas"))]


def test_the_blob_endpoint_must_be_https_except_for_a_loopback_emulator(tmp_path: Path) -> None:
    make_settings(tmp_path, **{**POSTGRES, "evidence_blob_url": "http://127.0.0.1:10000/devstoreaccount1"})
    for bad in ("http://stfabricops.blob.core.windows.net", "ftp://x", "stfabricops.blob.core.windows.net"):
        with pytest.raises(ValueError, match="must be an https URL"):
            make_settings(tmp_path, **{**POSTGRES, "evidence_blob_url": bad})


def test_pool_and_job_timing_guard_rails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="POOL_MIN"):
        make_settings(tmp_path, postgres_pool_min=9, postgres_pool_max=3)
    with pytest.raises(ValueError, match="postgres_pool_max"):
        make_settings(tmp_path, postgres_pool_max=1)  # migrations hold a lock on one connection and need a second
    with pytest.raises(ValueError, match="CLAIM_TTL"):
        make_settings(tmp_path, investigation_poll_interval_seconds=10, investigation_claim_ttl_seconds=20)
    with pytest.raises(ValueError, match="postgres_schema"):
        make_settings(tmp_path, postgres_schema="Robert'); DROP TABLE x;--")


async def test_the_container_refuses_a_deployed_environment_on_local_storage_before_touching_anything(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, deployment_environment="prod", api_auth_mode=ApiAuthMode.EASY_AUTH)
    with pytest.raises(ValueError, match="requires ORQENTIS_STORAGE_BACKEND=postgres"):
        await build_container(settings)
    assert not settings.data_dir.exists()


async def test_resetting_local_files_is_refused_for_the_postgres_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reset deletes local files only"):
        await build_container(make_settings(tmp_path, **POSTGRES), reset=True)


def test_the_contracts_blob_defaults_to_one_file_in_its_own_container(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, **POSTGRES)
    assert settings.contracts_path is None
    assert (settings.contracts_container, settings.contracts_blob_name) == ("contracts", "estate.json")
    custom = make_settings(tmp_path, **POSTGRES, contracts_container="estate-reviewed", contracts_blob_name="a/b.json")
    assert (custom.contracts_container, custom.contracts_blob_name) == ("estate-reviewed", "a/b.json")


@pytest.mark.security
def test_a_contracts_blob_location_that_could_escape_its_container_is_refused(tmp_path: Path) -> None:
    for name in ("../estate.json", "a/../b.json", "/estate.json", r"a\b.json", "", "x" * 1025):
        with pytest.raises(ValueError, match="contracts_blob_name"):
            make_settings(tmp_path, **POSTGRES, contracts_blob_name=name)
    for container in ("Contracts", "co", "-contracts", "contracts-", "a_b", "x" * 64):
        with pytest.raises(ValueError, match="contracts_container"):
            make_settings(tmp_path, **POSTGRES, contracts_container=container)


@pytest.mark.security
@pytest.mark.parametrize("environment", ["dev", "test", "prod"])
def test_a_database_connection_that_does_not_verify_the_server_is_for_a_local_container_only(
    tmp_path: Path, environment: str
) -> None:
    """The Entra access token is the password: ``require`` encrypts but accepts any certificate, so it would hand
    the token to whoever can intercept the connection."""
    for mode in (PostgresSslMode.REQUIRE, PostgresSslMode.DISABLE):
        with pytest.raises(ValueError, match="only permitted when deployment_environment=local"):
            deployed(tmp_path, deployment_environment=environment, postgres_ssl_mode=mode)
    deployed(tmp_path, deployment_environment=environment, postgres_ssl_mode=PostgresSslMode.VERIFY_FULL)
    make_settings(tmp_path, **POSTGRES, postgres_ssl_mode=PostgresSslMode.REQUIRE)  # a local container may


@pytest.mark.security
def test_the_token_scope_for_postgres_is_validated_like_the_fabric_scopes(tmp_path: Path) -> None:
    """The scope decides which resource the Entra token is issued for: a typo must not send it somewhere else."""
    make_settings(tmp_path, **POSTGRES, postgres_scope="https://ossrdbms-aad.database.windows.net/.default")
    for bad in (
        "https://ossrdbms-aad.database.windows.net",
        "http://evil.example/.default",
        "ossrdbms-aad/.default",
        "",
    ):
        with pytest.raises(ValueError, match="postgres_scope"):
            make_settings(tmp_path, **POSTGRES, postgres_scope=bad)


def test_the_epoch_container_and_the_role_to_act_as_are_validated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, **POSTGRES)
    assert settings.audit_epoch_container == "audit-epochs" and settings.postgres_set_role is None
    make_settings(tmp_path, **POSTGRES, audit_epoch_container="ep-1", postgres_set_role="id-fabricops-dev-reader")
    for bad in ("Epochs", "ep", "-ep", "ep_x"):
        with pytest.raises(ValueError, match="audit_epoch_container"):
            make_settings(tmp_path, **POSTGRES, audit_epoch_container=bad)
    for bad in ('x"; DROP ROLE y; --', "", "a b", "x" * 64):
        with pytest.raises(ValueError, match="postgres_set_role"):
            make_settings(tmp_path, **POSTGRES, postgres_set_role=bad)
