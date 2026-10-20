"""Blob storage adapters (evidence, reports, audit anchors). The only package that imports the Blob SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from azure.core.credentials_async import AsyncTokenCredential
from azure.storage.blob.aio import BlobServiceClient

from orqentis.storage.blob.anchors import BlobAuditAnchorStore
from orqentis.storage.blob.evidence import BlobEvidenceStore

__all__ = ["BlobAuditAnchorStore", "BlobEvidenceStore", "BlobStorage"]


@dataclass
class BlobStorage:
    """The evidence store and the anchor store on one storage account (or OneLake) endpoint."""

    service: BlobServiceClient
    evidence: BlobEvidenceStore
    anchors: BlobAuditAnchorStore

    @classmethod
    def open(
        cls,
        account_url: str,
        credential: AsyncTokenCredential,
        *,
        evidence_container: str = "evidence",
        evidence_prefix: str = "",
        anchor_container: str = "audit-anchors",
        anchor_prefix: str = "",
    ) -> Self:
        """Build the clients (no network traffic yet). ``credential`` is an Entra token credential, never a key."""
        service = BlobServiceClient(account_url, credential=credential)
        return cls(
            service=service,
            evidence=BlobEvidenceStore(service.get_container_client(evidence_container), path_prefix=evidence_prefix),
            anchors=BlobAuditAnchorStore(service.get_container_client(anchor_container), path_prefix=anchor_prefix),
        )

    async def close(self) -> None:
        await self.service.close()
