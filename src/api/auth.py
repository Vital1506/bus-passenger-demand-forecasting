"""Authentication and authorization introspection.

Sign-in itself stays in :mod:`src.security` (bearer keys, RBAC, agency scoping).
These endpoints expose what the caller may do and the redacted audit trail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .. import security
from .deps import app_module, require
from .pagination import Page, PageParams, page_params
from .storage import read_jsonl

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

AUDIT_SCAN_LIMIT = 5_000


class Capabilities(BaseModel):
    principal: dict[str, Any]
    workspace_agency: str
    permissions: list[str] = Field(default_factory=list)
    roles: dict[str, list[str]] = Field(default_factory=dict)
    policy: list[dict[str, str]] = Field(default_factory=list)
    rate_limit_per_minute: int
    auth_required: bool


class AuditEvent(BaseModel):
    recorded_at: str | None = None
    action: str | None = None
    outcome: str | None = None
    method: str | None = None
    path: str | None = None
    status_code: int | None = None
    principal_id: str | None = None
    agency: str | None = None
    detail: str | None = None


@router.get("/capabilities", response_model=Capabilities, dependencies=[Depends(require("data:read"))])
def capabilities(principal: security.Principal = Depends(require("data:read"))) -> Capabilities:
    """What this credential may do, plus the full role/permission matrix."""

    app = app_module()
    registry = app.security_registry()
    return Capabilities(
        principal=security.public_principal(principal),
        workspace_agency=str(registry.get("workspace_agency", principal.agency)),
        permissions=sorted(principal.permissions or security.ROLE_PERMISSIONS.get(principal.role, frozenset())),
        roles={role: sorted(values) for role, values in security.ROLE_PERMISSIONS.items()},
        policy=[
            {"method": method, "pattern": pattern, "permission": permission}
            for method, pattern, permission in app.SECURITY_POLICY
        ],
        rate_limit_per_minute=app.rate_limit_per_minute(),
        auth_required=app.auth_required(),
    )


@router.get("/audit", dependencies=[Depends(require("security:admin"))])
def audit_log(
    params: PageParams = Depends(page_params),
    action: str | None = Query(default=None, max_length=64),
    outcome: str | None = Query(default=None, max_length=32),
) -> Page[AuditEvent]:
    """Paginated, already-redacted security audit trail (newest first)."""

    path: Path = app_module().SECURITY_AUDIT_PATH
    records = read_jsonl(path)
    if action:
        records = [row for row in records if str(row.get("action")) == action]
    if outcome:
        records = [row for row in records if str(row.get("outcome")) == outcome]
    records = records[-AUDIT_SCAN_LIMIT:]
    events = [
        AuditEvent(
            recorded_at=row.get("recorded_at") or row.get("at"),
            action=row.get("action"),
            outcome=row.get("outcome"),
            method=row.get("method"),
            path=row.get("path"),
            status_code=row.get("status_code"),
            principal_id=row.get("principal_id"),
            agency=row.get("agency"),
            detail=row.get("detail"),
        )
        for row in reversed(records)
    ]
    return Page.build(events, params)
