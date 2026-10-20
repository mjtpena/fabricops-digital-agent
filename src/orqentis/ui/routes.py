"""The pages of the web UI (ADR-0018). Server-rendered, no JavaScript.

Every route carries the same authorisation guards as the API route that does the same thing (``orqentis.api.authz``):

* pages that read use ``ReadPrincipal``; audit verification uses ``VerifyPrincipal`` (it walks the whole hash chain);
* approve, reject and execute use ``DecidingPrincipal``: a HUMAN holding an approver role, never an application token
  and never a token carrying ``FabricOps.Automation``.

The decisions call the same service methods as the API (``ApprovalService.decide`` and ``ActionGateway.execute``), which
re-check kind, requester, role, expiry, fingerprint, single use and current policy. The UI adds no decision logic and no
privilege: buttons are shown when they could work, and a click that should not work is refused by the services.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, status
from starlette.responses import RedirectResponse, Response

from orqentis.actions.gateway import GatewayStatus
from orqentis.api.authz import DecidingPrincipal, ReadPrincipal, VerifyPrincipal, refuse
from orqentis.api.deps import ContainerDep
from orqentis.api.errors import ApiError
from orqentis.api.roles import APPROVER_ROLES, ROLE_AUTOMATION
from orqentis.api.schemas import DecisionRequest, IncidentSummary
from orqentis.domain.actions import ActionStatus
from orqentis.domain.approvals import ApprovalStatus
from orqentis.domain.incidents import IncidentStatus
from orqentis.domain.principals import Principal, PrincipalKind
from orqentis.domain.taxonomy import Severity
from orqentis.storage.incidents import IncidentQuery
from orqentis.ui import paths
from orqentis.ui.render import UiState, render_page

router = APIRouter(prefix=paths.UI_PREFIX, include_in_schema=False)

_MAX_ROWS = 200
_MAX_FORM_FIELDS = 8
_AUDIT_ROWS = 50
_FLASH = {
    "approved": "The request was approved.",
    "rejected": "The request was rejected.",
    "executed": "The action was executed.",
    "failed": "The action was attempted and failed. See the action result below.",
}


def _filter[E: StrEnum](enum_type: type[E], raw: str | None, name: str) -> E | None:
    if raw is None or not raw.strip():
        return None
    try:
        return enum_type(raw.strip().upper())
    except ValueError:
        raise ApiError(400, "INVALID_FILTER", f"Unknown {name}") from None


def can_decide(principal: Principal) -> bool:
    """Whether the UI offers decisions to this caller (the guards and services decide whether they work)."""
    return (
        principal.kind is PrincipalKind.HUMAN
        and ROLE_AUTOMATION not in principal.roles
        and bool(principal.roles & APPROVER_ROLES)
    )


# ------------------------------------------------------------------------------------------------- navigation
@router.get("")
async def ui_root(_: ReadPrincipal) -> RedirectResponse:
    return RedirectResponse(paths.incidents_path(), status_code=status.HTTP_303_SEE_OTHER)


@router.get("/assets/ui.css")
async def stylesheet(request: Request, _: ReadPrincipal) -> Response:
    ui: UiState = request.app.state.ui
    return Response(ui.stylesheet, media_type="text/css", headers={"Cache-Control": "private, max-age=3600"})


# --------------------------------------------------------------------------------------------------- incidents
@router.get("/incidents")
async def incident_list(
    request: Request,
    c: ContainerDep,
    principal: ReadPrincipal,
    status: str | None = None,
    severity: str | None = None,
) -> Response:
    wanted_status = _filter(IncidentStatus, status, "status")
    wanted_severity = _filter(Severity, severity, "severity")
    incidents = await c.incidents.repository.list(
        IncidentQuery(statuses=[wanted_status] if wanted_status else None, limit=500)
    )
    if wanted_severity is not None:
        incidents = [i for i in incidents if i.severity is wanted_severity]
    return render_page(
        request,
        "incidents.html",
        principal=principal,
        incidents=[IncidentSummary.of(i) for i in incidents[:_MAX_ROWS]],
        total=len(incidents),
        statuses=[s.value for s in IncidentStatus],
        severities=[s.value for s in sorted(Severity, key=lambda s: -s.rank)],
        selected_status=wanted_status.value if wanted_status else "",
        selected_severity=wanted_severity.value if wanted_severity else "",
        limit=_MAX_ROWS,
    )


@router.get("/incidents/{incident_id}")
async def incident_detail(incident_id: str, request: Request, c: ContainerDep, principal: ReadPrincipal) -> Response:
    incident = await c.incidents.get(incident_id)
    approvals = await c.approvals.list(incident_id=incident_id)
    return render_page(
        request,
        "incident.html",
        principal=principal,
        incident=incident,
        approvals=approvals,
        timeline=sorted(incident.timeline, key=lambda entry: entry.timestamp),
        recommendation=incident.recommended_actions[0] if incident.recommended_actions else None,
    )


@router.get("/incidents/{incident_id}/report")
async def incident_report(incident_id: str, request: Request, c: ContainerDep, principal: ReadPrincipal) -> Response:
    incident = await c.incidents.get(incident_id)
    return render_page(request, "report.html", principal=principal, incident=incident, report=c.reports.build(incident))


# --------------------------------------------------------------------------------------------------- approvals
@router.get("/approvals")
async def approval_list(
    request: Request, c: ContainerDep, principal: ReadPrincipal, status: str | None = None
) -> Response:
    wanted = _filter(ApprovalStatus, status, "status")
    approvals = sorted(await c.approvals.list(status=wanted), key=lambda a: a.requested_at, reverse=True)
    return render_page(
        request,
        "approvals.html",
        principal=principal,
        approvals=approvals[:_MAX_ROWS],
        total=len(approvals),
        statuses=[s.value for s in ApprovalStatus],
        selected_status=wanted.value if wanted else "",
        limit=_MAX_ROWS,
    )


@router.get("/approvals/{approval_id}")
async def approval_detail(
    approval_id: str, request: Request, c: ContainerDep, principal: ReadPrincipal, done: str | None = None
) -> Response:
    approval = await c.approvals.get(approval_id)
    action = approval.proposed_action
    incident = await c.incidents.repository.get(approval.incident_id)
    holds_role = principal.has_role(approval.required_role)
    decider = can_decide(principal)
    pending = approval.status is ApprovalStatus.PENDING
    return render_page(
        request,
        "approval.html",
        principal=principal,
        approval=approval,
        action=action,
        incident=incident,
        flash=_FLASH.get(done or ""),
        offer_decision=decider and pending and holds_role and principal.id != approval.requested_by,
        is_requester=principal.id == approval.requested_by,
        holds_role=holds_role,
        is_decider=decider,
        offer_execute=(
            decider
            and approval.status is ApprovalStatus.APPROVED
            and holds_role
            and action.status is ActionStatus.APPROVED
            and c.settings.enable_write_actions
        ),
        write_enabled=c.settings.enable_write_actions,
    )


async def _read_form(request: Request) -> dict[str, str]:
    """The URL-encoded fields of a UI form (the only body a UI route accepts)."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "The form must be application/x-www-form-urlencoded")
    try:
        text = (await request.body()).decode("utf-8")
        parsed = parse_qs(text, keep_blank_values=True, max_num_fields=_MAX_FORM_FIELDS)
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "BAD_FORM", "The form could not be read") from None
    # A repeated field is ambiguous (a smuggled second token or comment): refuse it instead of picking one.
    if any(len(values) != 1 for values in parsed.values()):
        raise ApiError(400, "BAD_FORM", "The form repeats a field")
    return {name: values[0] for name, values in parsed.items()}


async def _checked_form(request: Request, principal: Principal) -> dict[str, str]:
    form = await _read_form(request)
    ui: UiState = request.app.state.ui
    if not ui.csrf.verify(form.get("csrf_token"), principal.id):
        await refuse(
            request,
            principal,
            "CSRF_TOKEN_INVALID",
            "The form's security token is missing, wrong or expired. Reload the page and try again.",
        )
    return form


def _comment(form: dict[str, str]) -> str | None:
    raw = form.get("comment", "").strip()
    try:
        return DecisionRequest(comment=raw or None).comment  # the API's own limits (1000 characters)
    except ValueError:
        raise ApiError(422, "INVALID_COMMENT", "The comment is too long (1000 characters at most)") from None


def _back(approval_id: str, done: str) -> RedirectResponse:
    return RedirectResponse(f"{paths.approval_path(approval_id)}?done={done}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str, request: Request, c: ContainerDep, principal: DecidingPrincipal) -> Response:
    comment = _comment(await _checked_form(request, principal))
    await c.approvals.decide(approval_id, principal=principal, approve=True, comment=comment)
    return _back(approval_id, "approved")


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str, request: Request, c: ContainerDep, principal: DecidingPrincipal) -> Response:
    comment = _comment(await _checked_form(request, principal))
    await c.approvals.decide(approval_id, principal=principal, approve=False, comment=comment)
    return _back(approval_id, "rejected")


@router.post("/approvals/{approval_id}/execute")
async def execute(approval_id: str, request: Request, c: ContainerDep, principal: DecidingPrincipal) -> Response:
    await _checked_form(request, principal)
    outcome = await c.gateway.execute(approval_id, principal=principal)
    return _back(approval_id, "executed" if outcome.status is GatewayStatus.EXECUTED else "failed")


# ------------------------------------------------------------------------------------------------------ audit
@router.get("/audit")
async def audit_page(request: Request, c: ContainerDep, principal: VerifyPrincipal) -> Response:
    verification = await c.audit.verify()
    recent = await c.audit.list(limit=_AUDIT_ROWS)
    return render_page(
        request, "audit.html", principal=principal, verification=verification, records=list(reversed(recent))
    )
