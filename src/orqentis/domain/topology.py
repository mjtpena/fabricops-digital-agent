"""Dependency edges between estate nodes."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from orqentis.domain.common import DomainModel, Metadata


class DependencyType(StrEnum):
    TRIGGERS = "TRIGGERS"
    READS_FROM = "READS_FROM"
    WRITES_TO = "WRITES_TO"
    REFRESHES = "REFRESHES"
    DEPENDS_ON = "DEPENDS_ON"
    PRODUCES = "PRODUCES"
    CONSUMES = "CONSUMES"

    @property
    def source_is_upstream(self) -> bool:
        """Direction of data/control flow for ``source -[type]-> target``.

        ``A TRIGGERS/WRITES_TO/REFRESHES/PRODUCES B`` means A is upstream of B.
        ``A READS_FROM/DEPENDS_ON/CONSUMES B`` means B is upstream of A.
        """
        return self in {
            DependencyType.TRIGGERS,
            DependencyType.WRITES_TO,
            DependencyType.REFRESHES,
            DependencyType.PRODUCES,
        }


class Dependency(DomainModel):
    id: str
    tenant_id: str
    source_item_id: str
    target_item_id: str
    dependency_type: DependencyType
    source: str = "declared"
    metadata: Metadata = Field(default_factory=dict)

    @property
    def upstream_id(self) -> str:
        return self.source_item_id if self.dependency_type.source_is_upstream else self.target_item_id

    @property
    def downstream_id(self) -> str:
        return self.target_item_id if self.dependency_type.source_is_upstream else self.source_item_id
