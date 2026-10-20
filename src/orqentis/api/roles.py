"""Entra ID app roles of the FabricOps API and which API operations each one unlocks.

Approval roles (``Operator``, ``PlatformAdmin``) are also ranked by the policy file
(``role_hierarchy``). ``Automation`` and ``Reader`` are API-only roles. They must never be added to that
hierarchy: neither can decide or execute an approval.
"""

from __future__ import annotations

ROLE_OPERATOR = "FabricOps.Operator"
ROLE_PLATFORM_ADMIN = "FabricOps.PlatformAdmin"
# Application members only (``allowedMemberTypes: ["Application"]``): schedulers, smoke tests, other services.
ROLE_AUTOMATION = "FabricOps.Automation"
# Read-only access for people (or applications) who must not trigger anything.
ROLE_READER = "FabricOps.Reader"

APPROVER_ROLES = frozenset({ROLE_OPERATOR, ROLE_PLATFORM_ADMIN})

# GET endpoints (incidents, evidence, runbooks, estate, approvals, audit).
READ_ROLES = frozenset({ROLE_READER, ROLE_AUTOMATION, ROLE_OPERATOR, ROLE_PLATFORM_ADMIN})
# POST /telemetry/collect: read Fabric and run the deterministic detection rules.
COLLECT_ROLES = frozenset({ROLE_AUTOMATION, ROLE_OPERATOR, ROLE_PLATFORM_ADMIN})
# POST /incidents/{id}/investigate: run the investigator (may call the LLM, may open approval requests).
INVESTIGATE_ROLES = APPROVER_ROLES
