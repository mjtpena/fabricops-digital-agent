"""Write-capability adapters. Only the ActionGateway holds one of these."""

from __future__ import annotations

from typing import Any

from orqentis.config.clock import Clock
from orqentis.domain.actions import Action
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.fabric.errors import WriteCapabilityNotGrantedError


class DisabledFabricWriteClient:
    """Default: the write boundary is closed. FabricOps is read-only unless explicitly enabled."""

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
        raise WriteCapabilityNotGrantedError(
            "Write capability is not granted to this deployment (ORQENTIS_ENABLE_WRITE_ACTIONS=false)",
            error_code="WriteCapabilityNotGranted",
            details={"action_id": action.action_id, "action_type": action.action_type.value},
        )


class MockFabricWriteClient:
    """Records approved mutations against the synthetic estate instead of performing them."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.executed: list[tuple[Action, ApprovalRequest]] = []

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
        # Defence in depth: the gateway already checked this.
        if approval.status is not ApprovalStatus.APPROVED or approval.proposed_action.action_id != action.action_id:
            raise WriteCapabilityNotGrantedError("Mutation attempted without a matching approved approval")
        self.executed.append((action, approval))
        return {
            "simulated": True,
            "action_type": action.action_type.value,
            "target_item_id": action.target_item_id,
            "executed_at": self._clock.now().isoformat(),
        }
