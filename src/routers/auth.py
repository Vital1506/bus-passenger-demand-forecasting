"""Authentication and RBAC inspection router."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Header

from ..auth import AuthenticatedPrincipal, UserRole, resolve_principal

auth_router = APIRouter(tags=["auth"])


@auth_router.get("/api/auth/roles")
def get_auth_roles(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Inspect caller's effective RBAC role and platform capabilities."""
    principal = resolve_principal(authorization)
    role = principal.role
    permissions = {
        UserRole.PUBLIC: ["read_public_demo", "read_gtfs_schedules", "read_health"],
        UserRole.ANALYST: [
            "read_public_demo",
            "read_gtfs_schedules",
            "read_health",
            "read_forecasts",
            "read_realtime_telemetry",
            "read_operations_board",
            "read_occupancy",
            "read_reports",
        ],
        UserRole.OPERATOR: [
            "read_public_demo",
            "read_gtfs_schedules",
            "read_health",
            "read_forecasts",
            "read_realtime_telemetry",
            "read_operations_board",
            "read_occupancy",
            "read_reports",
            "upload_datasets",
            "trigger_model_training",
            "simulate_scenarios",
            "ingest_telemetry",
        ],
        UserRole.ADMINISTRATOR: [
            "read_public_demo",
            "read_gtfs_schedules",
            "read_health",
            "read_forecasts",
            "read_realtime_telemetry",
            "read_operations_board",
            "read_occupancy",
            "read_reports",
            "upload_datasets",
            "trigger_model_training",
            "simulate_scenarios",
            "ingest_telemetry",
            "manage_users",
            "manage_agencies",
            "manage_gtfs_feeds",
            "trigger_system_benchmark",
            "send_webhooks",
        ],
    }

    return {
        "authenticated": principal.is_authenticated,
        "role": role.value,
        "agency": principal.agency,
        "key_id": principal.key_id,
        "permissions": permissions.get(role, permissions[UserRole.PUBLIC]),
    }
