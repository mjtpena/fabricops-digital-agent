"""A snapshot of the workspaces and items that exist, used to check contracts against reality.

``orqentis contracts scaffold --catalog-output`` writes one from a live listing; ``contracts validate
--catalog`` reads it. A full estate file (``fixtures/fabric/estate.json``) works too: only its
``workspaces`` and ``items`` sections are read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orqentis.contracts.diagnostics import parse_json, pydantic_violations, read_text
from orqentis.contracts.errors import ContractsInvalidError, ContractViolation
from orqentis.domain.estate import Capacity, FabricItem, Workspace

CATALOG_KEYS = ("workspaces", "items", "capacities")


class CatalogInvalidError(ContractsInvalidError):
    summary = "Catalog file is invalid"


class CatalogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaces: list[Workspace] = Field(default_factory=list)
    items: list[FabricItem] = Field(default_factory=list)
    capacities: list[Capacity] = Field(default_factory=list)


@dataclass(frozen=True)
class CatalogSnapshot:
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    items: dict[str, FabricItem] = field(default_factory=dict)
    capacities: dict[str, Capacity] = field(default_factory=dict)

    @classmethod
    def of(
        cls, workspaces: list[Workspace], items: list[FabricItem], capacities: list[Capacity] | None = None
    ) -> CatalogSnapshot:
        return cls({w.id: w for w in workspaces}, {i.id: i for i in items}, {c.id: c for c in capacities or []})

    @classmethod
    def load(cls, path: Path) -> CatalogSnapshot:
        text = read_text(path)
        raw, locations, violations = parse_json(text)
        if not violations and not isinstance(raw, dict):
            violations.append(ContractViolation("", "the catalog must be a JSON object"))
        if violations:
            raise CatalogInvalidError(path, tuple(violations))
        try:
            payload = CatalogPayload.model_validate({k: raw[k] for k in CATALOG_KEYS if k in raw})
        except ValidationError as exc:
            raise CatalogInvalidError(path, tuple(pydantic_violations(exc, locations))) from None
        return cls.of(payload.workspaces, payload.items, payload.capacities)

    def to_json(self) -> str:
        payload = CatalogPayload(
            workspaces=list(self.workspaces.values()),
            items=list(self.items.values()),
            capacities=list(self.capacities.values()),
        )
        document = payload.model_dump(mode="json", exclude_none=True)
        return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
