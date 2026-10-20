"""The contracts file: what platform teams declare about an estate (version-controlled, human-reviewed)."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from orqentis.domain.estate import DataProduct, EnvironmentDeclaration, ExternalSource
from orqentis.domain.sla import SLA
from orqentis.domain.topology import Dependency

# A full synthetic estate file (fixtures/fabric/estate.json) is also accepted as a contracts file: these
# sections describe the estate itself and are ignored when it is read as contracts.
ESTATE_ONLY_KEYS = frozenset({"tenant", "capacities", "workspaces", "items", "deployment_states"})

SCHEMA_KEY = "$schema"


class EstateContracts(BaseModel):
    """What platform teams declare (version-controlled): environments, sources, topology, data products, SLAs.

    Fabric cannot tell FabricOps its lineage, which systems feed it, which tables are data products or when
    they must be fresh, and workspace names are only a heuristic for PROD/TEST/DEV. These sections are
    reviewed like code. ``orqentis contracts scaffold`` drafts a first version; ``contracts validate`` checks it.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_ref: str | None = Field(
        default=None,
        alias=SCHEMA_KEY,
        description="Optional reference to the JSON Schema, for editor validation and completion.",
    )
    environments: list[EnvironmentDeclaration] = Field(
        default_factory=list,
        description="Declared environments (by workspace id, workspace name pattern or item id). "
        "They take precedence over inference from names and drive incident severity and policy.",
    )
    external_sources: list[ExternalSource] = Field(
        default_factory=list, description="Systems outside Fabric that feed the estate."
    )
    data_products: list[DataProduct] = Field(
        default_factory=list, description="Logical, owned data assets backed by Fabric items."
    )
    dependencies: list[Dependency] = Field(
        default_factory=list, description="Lineage and control-flow edges between estate nodes."
    )
    slas: list[SLA] = Field(default_factory=list, description="Availability deadlines of data products.")

    _source_sha256: str | None = PrivateAttr(default=None)

    @property
    def source_sha256(self) -> str | None:
        """SHA-256 of the file these contracts were loaded from (None for contracts built in code)."""
        return self._source_sha256

    def with_source_sha256(self, digest: str) -> Self:
        self._source_sha256 = digest
        return self


CONTRACT_SECTIONS: tuple[str, ...] = (
    "environments",
    "external_sources",
    "data_products",
    "dependencies",
    "slas",
)
