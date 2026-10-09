"""Who is acting: humans, the agent, or the platform itself."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from orqentis.domain.common import DomainModel


class PrincipalKind(StrEnum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"
    SERVICE = "SERVICE"


class Principal(DomainModel):
    id: str
    display_name: str
    kind: PrincipalKind
    roles: frozenset[str] = Field(default_factory=frozenset)

    def has_role(self, role: str | None) -> bool:
        return role is None or role in self.roles


AGENT_PRINCIPAL = Principal(
    id="agent:FabricOpsInvestigator", display_name="FabricOps Investigator", kind=PrincipalKind.AGENT
)
SYSTEM_PRINCIPAL = Principal(id="system:orqentis", display_name="Orqentis FabricOps", kind=PrincipalKind.SERVICE)
