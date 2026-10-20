"""Fixtures that run the application on PostgreSQL + Azurite. Import them into the conftest that uses them."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from azure.storage.blob.aio import BlobServiceClient

from tests.support.postgres_app import AppStorage, make_app_storage
from tests.support.services import AzuriteServer, PostgresServer


@pytest.fixture
async def app_storage(
    postgres_server: PostgresServer,
    azurite_server: AzuriteServer,
    blob_service: BlobServiceClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AppStorage]:
    async for storage in make_app_storage(postgres_server, azurite_server, blob_service, monkeypatch):
        yield storage
