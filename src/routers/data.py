"""Passenger ridership data upload and ingestion router."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile

from ..auth import UserRole, require_role

data_router = APIRouter(tags=["data"])


@data_router.get("/api/data/status", dependencies=[Depends(require_role(UserRole.PUBLIC))])
def get_data_service_status() -> dict[str, Any]:
    """Data ingestion subsystem health and capability status."""
    return {
        "status": "ready",
        "supported_formats": ["csv", "json"],
        "apc_supported": True,
        "gtfs_supported": True,
    }
