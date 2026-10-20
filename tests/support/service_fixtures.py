"""Fixtures for tests that need PostgreSQL and/or Azurite. Import them into the conftest that uses them."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from azure.storage.blob.aio import BlobServiceClient, ContainerClient

from orqentis.config.clock import SystemClock
from orqentis.storage.blob import BlobAuditAnchorStore, BlobEpochMarkerStore, BlobEvidenceStore
from orqentis.storage.postgres import PostgresConfig, PostgresStorage
from tests.support.services import AzuriteServer, PostgresServer, start_azurite, start_postgres


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[PostgresServer]:
    yield from start_postgres()


@pytest.fixture(scope="session")
def azurite_server() -> Iterator[AzuriteServer]:
    yield from start_azurite()


def make_config(server: PostgresServer, schema: str, **overrides: object) -> PostgresConfig:
    values: dict[str, object] = {
        "host": server.host,
        "port": server.port,
        "user": server.user,
        "database": server.database,
        "schema": schema,
        "auth": "none",  # trust authentication: no password anywhere
        "ssl_mode": "disable",
        "pool_min": 1,
        "pool_max": 12,
    }
    values.update(overrides)
    return PostgresConfig(**values)  # type: ignore[arg-type]


async def drop_schema(server: PostgresServer, schema: str) -> None:
    conn = await asyncpg.connect(host=server.host, port=server.port, user=server.user, database=server.database)
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await conn.close()


@pytest.fixture
async def pg_config(postgres_server: PostgresServer) -> AsyncIterator[PostgresConfig]:
    """A configuration for a schema that exists only for this test."""
    schema = f"t_{secrets.token_hex(6)}"
    yield make_config(postgres_server, schema)
    await drop_schema(postgres_server, schema)


@pytest.fixture
async def pg(pg_config: PostgresConfig) -> AsyncIterator[PostgresStorage]:
    storage = await PostgresStorage.open(pg_config, None, SystemClock())
    try:
        yield storage
    finally:
        await storage.close()


@pytest.fixture
async def blob_service(azurite_server: AzuriteServer) -> AsyncIterator[BlobServiceClient]:
    # The emulator is addressed with the key generated for this run; production code never sees a key.
    service = BlobServiceClient(
        azurite_server.url,
        credential={"account_name": azurite_server.account, "account_key": azurite_server.key},
    )
    try:
        yield service
    finally:
        await service.close()


@pytest.fixture
async def blob_evidence(blob_service: BlobServiceClient) -> BlobEvidenceStore:
    name = f"ev-{secrets.token_hex(5)}"
    await blob_service.create_container(name)
    return BlobEvidenceStore(blob_service.get_container_client(name))


@pytest.fixture
async def blob_anchors(blob_service: BlobServiceClient) -> BlobAuditAnchorStore:
    name = f"an-{secrets.token_hex(5)}"
    await blob_service.create_container(name)
    return BlobAuditAnchorStore(blob_service.get_container_client(name))


@pytest.fixture
async def blob_contracts_container(blob_service: BlobServiceClient) -> ContainerClient:
    name = f"co-{secrets.token_hex(5)}"
    await blob_service.create_container(name)
    return blob_service.get_container_client(name)


@pytest.fixture
async def blob_epochs(blob_service: BlobServiceClient) -> BlobEpochMarkerStore:
    name = f"ep-{secrets.token_hex(5)}"
    await blob_service.create_container(name)
    return BlobEpochMarkerStore(blob_service.get_container_client(name))
