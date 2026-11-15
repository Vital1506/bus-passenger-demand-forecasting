"""GTFS transit network schedule management router."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..auth import UserRole, require_role
from ..gtfs_importer import GTFSValidationError, import_gtfs_feed
from ..gtfs_service import (
    get_route_details as gtfs_get_route_details,
    list_agencies as gtfs_list_agencies,
    list_feeds as gtfs_list_feeds,
    list_routes as gtfs_list_routes,
    list_stops as gtfs_list_stops,
)
from ..db.session import get_db_session

gtfs_router = APIRouter(tags=["gtfs"])


@gtfs_router.post("/api/gtfs/import", dependencies=[Depends(require_role(UserRole.ADMINISTRATOR))])
async def import_gtfs_feed(
    file: UploadFile,
    agency_id: str = "default",
    feed_version: str | None = None,
) -> dict[str, Any]:
    """Import a GTFS ZIP archive containing transit schedule tables."""
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are supported for GTFS imports")
    content = await file.read()
    return import_gtfs_zip(
        zip_bytes=content,
        agency_id=agency_id,
        feed_version=feed_version,
    )


@gtfs_router.get("/api/gtfs/feeds", dependencies=[Depends(require_role(UserRole.PUBLIC))])
def list_gtfs_feeds() -> list[dict[str, Any]]:
    """List imported GTFS feeds and version metadata."""
    with get_db_session() as session:
        return gtfs_list_feeds(session)


@gtfs_router.get("/api/gtfs/agencies", dependencies=[Depends(require_role(UserRole.PUBLIC))])
def list_gtfs_agencies(feed_id: str | None = None) -> list[dict[str, Any]]:
    """List transit agencies defined in imported schedules."""
    with get_db_session() as session:
        return gtfs_list_agencies(session, feed_id=feed_id)


@gtfs_router.get("/api/gtfs/routes", dependencies=[Depends(require_role(UserRole.PUBLIC))])
def list_gtfs_routes(
    feed_id: str | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    """List transit routes with origins, destinations, and modes."""
    with get_db_session() as session:
        return gtfs_list_routes(session, agency_id=agency_id)


@gtfs_router.get("/api/gtfs/routes/{route_id}", dependencies=[Depends(require_role(UserRole.PUBLIC))])
def get_gtfs_route_details(route_id: str, feed_id: str | None = None) -> dict[str, Any]:
    """Detailed metadata for a specific route including stops and headways."""
    with get_db_session() as session:
        route = gtfs_get_route_details(session, route_id=route_id)
        if route is None:
            raise HTTPException(status_code=404, detail=f"Route '{route_id}' not found in GTFS schedule")
        return route


@gtfs_router.get("/api/gtfs/stops", dependencies=[Depends(require_role(UserRole.PUBLIC))])
def list_gtfs_stops(
    route_id: str | None = None,
    feed_id: str | None = None,
) -> list[dict[str, Any]]:
    """List scheduled transit stops with geographic coordinates."""
    with get_db_session() as session:
        return gtfs_list_stops(session)
