"""GTFS-Realtime (GTFS-RT) & Telemetry Ingestion Engine.

Provides real-time parsing, validation, spatial indexing, and caching for:
- VehiclePositions (live GPS coordinates, bearing, speed, occupancy status)
- TripUpdates (delays, arrival/departure predictions, schedule adherence)
- ServiceAlerts (disruptions, detours, active periods, affected entities)
"""

from .models import (
    OccupancyStatus,
    VehicleStatus,
    VehiclePosition,
    StopTimeUpdate,
    TripUpdate,
    ServiceAlert,
    RealtimeFeedMessage,
)
from .service import RealtimeService, get_realtime_service

__all__ = [
    "OccupancyStatus",
    "VehicleStatus",
    "VehiclePosition",
    "StopTimeUpdate",
    "TripUpdate",
    "ServiceAlert",
    "RealtimeFeedMessage",
    "RealtimeService",
    "get_realtime_service",
]
