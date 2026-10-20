"""Run the whole application (``build_container``) on the PostgreSQL storage backend against local containers.

The only thing replaced is the construction of the Blob client: production builds it with an Entra token credential,
which Azurite cannot validate over plain HTTP, so the tests hand the same adapters a client that signs with the key
generated for this run (``tests/support/services.py``). Everything else -- the settings, the bootstrap wiring, the
PostgreSQL adapters, migrations, the lease, the job store -- is the production code path.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from azure.core.credentials_async import AsyncTokenCredential
from azure.storage.blob.aio import BlobServiceClient

from orqentis.config.settings import Settings
from orqentis.storage.blob import BlobAuditAnchorStore, BlobEvidenceStore, BlobStorage
from tests.conftest import make_settings
from tests.support.service_fixtures import drop_schema
from tests.support.services import AzuriteServer, PostgresServer


@dataclass(frozen=True)
class AppStorage:
    """One isolated PostgreSQL schema and pair of Blob containers, shared by every instance of a test."""

    server: PostgresServer
    azurite: AzuriteServer
    schema: str
    evidence_container: str
    anchor_container: str

    def settings(self, tmp_path: Path, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "storage_backend": "postgres",
            "postgres_host": self.server.host,
            "postgres_port": self.server.port,
            "postgres_user": self.server.user,
            "postgres_database": self.server.database,
            "postgres_schema": self.schema,
            "postgres_auth": "none",  # trust authentication in the throwaway container: no password anywhere
            "postgres_ssl_mode": "disable",
            "evidence_blob_url": self.azurite.url,
            "evidence_container": self.evidence_container,
            "audit_anchor_container": self.anchor_container,
            "postgres_pool_max": 6,
        }
        values.update(overrides)
        return make_settings(tmp_path, **values)


def azurite_blob_storage(azurite: AzuriteServer) -> Any:
    """A replacement for ``BlobStorage.open`` that talks to Azurite with the per-run key."""

    def open_(
        account_url: str,
        credential: AsyncTokenCredential,
        *,
        evidence_container: str = "evidence",
        evidence_prefix: str = "",
        anchor_container: str = "audit-anchors",
        anchor_prefix: str = "",
    ) -> BlobStorage:
        service = BlobServiceClient(
            azurite.url, credential={"account_name": azurite.account, "account_key": azurite.key}
        )
        return BlobStorage(
            service=service,
            evidence=BlobEvidenceStore(service.get_container_client(evidence_container), path_prefix=evidence_prefix),
            anchors=BlobAuditAnchorStore(service.get_container_client(anchor_container), path_prefix=anchor_prefix),
        )

    return open_


async def make_app_storage(
    server: PostgresServer, azurite: AzuriteServer, service: BlobServiceClient, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AppStorage]:
    suffix = secrets.token_hex(5)
    storage = AppStorage(server, azurite, f"t_{suffix}", f"ev-{suffix}", f"an-{suffix}")
    await service.create_container(storage.evidence_container)
    await service.create_container(storage.anchor_container)
    monkeypatch.setattr(BlobStorage, "open", staticmethod(azurite_blob_storage(azurite)))
    yield storage
    await drop_schema(server, storage.schema)
