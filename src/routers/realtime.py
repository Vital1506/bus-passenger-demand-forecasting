"""GTFS-Realtime telemetry ingestion and streaming query router."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import UserRole, require_role
from ..realtime import get_realtime_service

realtime_router = APIRouter(tags=["realtime"])


class TelemetryIngestPayload(BaseModel):
    agency_id: str = Field(default="default", max_length=64)
    feed_type: str = Field(..., max_length=32)
    payload_base64: str = Field(..., min_length=1)


@realtime_router.get("/api/realtime/vehicles", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_live_vehicles(
    route_id: str | None = None,
    min_lat: float | None = None,
    max_lat: float | None = None,
    min_lon: float | None = None,
    max_lon: float | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    """Query live vehicle positions and current passenger occupancy."""
    rt = get_realtime_service()
    return rt.get_vehicles(
        route_id=route_id,
        min_lat=min_lat,
        max_lat=max_lat,
        min_lon=min_lon,
        max_lon=max_lon,
        agency_id=agency_id,
    )


@realtime_router.get("/api/realtime/trip-updates", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_live_trip_updates(
    route_id: str | None = None,
    trip_id: str | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    """Query real-time delays, schedule progress, and vehicle assignments."""
    rt = get_realtime_service()
    return rt.get_trip_updates(route_id=route_id, trip_id=trip_id, agency_id=agency_id)


@realtime_router.get("/api/realtime/alerts", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_live_service_alerts(
    route_id: str | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    """Query active transit service disruptions and passenger notices."""
    rt = get_realtime_service()
    return rt.get_alerts(route_id=route_id, agency_id=agency_id)


@realtime_router.post("/api/realtime/ingest", dependencies=[Depends(require_role(UserRole.OPERATOR))])
def ingest_realtime_feed(payload: TelemetryIngestPayload) -> dict[str, Any]:
    """Ingest GTFS-Realtime protocol buffer payload."""
    import base64
    rt = get_realtime_service()
    raw_bytes = base64.b64decode(payload.payload_base64)
    stats = rt.ingest_raw_protobuf(
        payload_bytes=raw_bytes,
        feed_type=payload.feed_type,
        agency_id=payload.agency_id,
    )
    return {"status": "ok", "ingest_statistics": stats}


@realtime_router.post("/api/realtime/demo-snapshot", dependencies=[Depends(require_role(UserRole.OPERATOR))])
def generate_demo_telemetry_snapshot(agency_id: str = "default") -> dict[str, Any]:
    """Generate deterministic synthetic telemetry snapshot for demo and testing."""
    rt = get_realtime_service()
    stats = rt.generate_synthetic_live_snapshot(agency_id=agency_id)
    return {"status": "ok", "snapshot_statistics": stats}
