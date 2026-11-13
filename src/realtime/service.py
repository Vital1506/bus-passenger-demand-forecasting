"""Service layer coordinating GTFS-RT ingestion, live queries, and fleet health."""

from __future__ import annotations

import time
from typing import Any

from .feed_ingester import RealtimeTelemetryStore
from .models import (
    OccupancyStatus,
    RealtimeFeedMessage,
    ServiceAlert,
    StopTimeUpdate,
    TripUpdate,
    VehiclePosition,
    VehicleStatus,
)
from .protobuf_parser import parse_realtime_feed

_GLOBAL_REALTIME_STORE: RealtimeTelemetryStore | None = None


def get_realtime_service() -> RealtimeService:
    """Return singleton instance of RealtimeService."""
    global _GLOBAL_REALTIME_STORE
    if _GLOBAL_REALTIME_STORE is None:
        _GLOBAL_REALTIME_STORE = RealtimeTelemetryStore()
    return RealtimeService(_GLOBAL_REALTIME_STORE)


class RealtimeService:
    def __init__(self, store: RealtimeTelemetryStore):
        self.store = store

    def ingest_payload(self, data: bytes | str | dict, agency_id: str = "default") -> dict[str, Any]:
        """Parse raw Protobuf or JSON GTFS-RT payload and update live store."""
        feed = parse_realtime_feed(data, agency_id=agency_id)
        result = self.store.ingest_feed(feed)
        result["feed_timestamp"] = feed.feed_timestamp
        result["gtfs_version"] = feed.gtfs_realtime_version
        return result

    def get_vehicles(
        self,
        route_id: str | None = None,
        min_lat: float | None = None,
        max_lat: float | None = None,
        min_lon: float | None = None,
        max_lon: float | None = None,
        agency_id: str | None = None,
    ) -> list[dict[str, Any]]:
        vehicles = self.store.get_vehicles(
            route_id=route_id,
            min_lat=min_lat,
            max_lat=max_lat,
            min_lon=min_lon,
            max_lon=max_lon,
            agency_id=agency_id,
        )
        return [v.to_dict() for v in vehicles]

    def get_vehicle(self, vehicle_id: str) -> dict[str, Any] | None:
        v = self.store.get_vehicle(vehicle_id)
        return v.to_dict() if v else None

    def get_trip_updates(
        self,
        route_id: str | None = None,
        trip_id: str | None = None,
        agency_id: str | None = None,
    ) -> list[dict[str, Any]]:
        updates = self.store.get_trip_updates(route_id=route_id, trip_id=trip_id, agency_id=agency_id)
        return [u.to_dict() for u in updates]

    def get_alerts(
        self,
        route_id: str | None = None,
        stop_id: str | None = None,
        agency_id: str | None = None,
    ) -> list[dict[str, Any]]:
        alerts = self.store.get_alerts(route_id=route_id, stop_id=stop_id, agency_id=agency_id)
        return [a.to_dict() for a in alerts]

    def get_summary(self, agency_id: str | None = None) -> dict[str, Any]:
        return self.store.get_fleet_summary(agency_id=agency_id)

    def generate_synthetic_live_snapshot(self, agency_id: str = "CTA") -> dict[str, Any]:
        """Generate a labeled, synthetic live snapshot for UI demonstration when no live provider feed is configured."""
        now = int(time.time())
        # Sample vehicles along representative Chicago coordinates
        sample_vehicles = [
            VehiclePosition(
                vehicle_id="CTA-4102",
                trip_id="T-1001",
                route_id="1",
                latitude=41.8789,
                longitude=-87.6359,
                bearing=180.0,
                speed_mps=8.5,
                current_status=VehicleStatus.IN_TRANSIT_TO,
                current_stop_sequence=4,
                stop_id="S-002",
                occupancy_status=OccupancyStatus.MANY_SEATS_AVAILABLE,
                occupancy_percentage=35,
                timestamp=now,
                agency_id=agency_id,
            ),
            VehiclePosition(
                vehicle_id="CTA-4105",
                trip_id="T-1002",
                route_id="1",
                latitude=41.8845,
                longitude=-87.6278,
                bearing=90.0,
                speed_mps=11.2,
                current_status=VehicleStatus.STOPPED_AT,
                current_stop_sequence=8,
                stop_id="S-003",
                occupancy_status=OccupancyStatus.STANDING_ROOM_ONLY,
                occupancy_percentage=85,
                timestamp=now,
                agency_id=agency_id,
            ),
            VehiclePosition(
                vehicle_id="CTA-4210",
                trip_id="T-2001",
                route_id="2",
                latitude=41.8661,
                longitude=-87.6169,
                bearing=135.0,
                speed_mps=6.8,
                current_status=VehicleStatus.IN_TRANSIT_TO,
                current_stop_sequence=2,
                stop_id="S-004",
                occupancy_status=OccupancyStatus.FEW_SEATS_AVAILABLE,
                occupancy_percentage=60,
                timestamp=now,
                agency_id=agency_id,
            ),
        ]

        sample_updates = [
            TripUpdate(
                trip_id="T-1001",
                route_id="1",
                vehicle_id="CTA-4102",
                delay=120,  # 2m late
                timestamp=now,
                agency_id=agency_id,
                stop_time_updates=[
                    StopTimeUpdate(stop_sequence=4, stop_id="S-002", arrival_delay=120, departure_delay=120),
                    StopTimeUpdate(stop_sequence=5, stop_id="S-003", arrival_delay=140, departure_delay=140),
                ],
            ),
            TripUpdate(
                trip_id="T-1002",
                route_id="1",
                vehicle_id="CTA-4105",
                delay=-45,  # 45s early
                timestamp=now,
                agency_id=agency_id,
                stop_time_updates=[
                    StopTimeUpdate(stop_sequence=8, stop_id="S-003", arrival_delay=-45, departure_delay=-30),
                ],
            ),
            TripUpdate(
                trip_id="T-2001",
                route_id="2",
                vehicle_id="CTA-4210",
                delay=420,  # 7m late (delayed)
                timestamp=now,
                agency_id=agency_id,
                stop_time_updates=[
                    StopTimeUpdate(stop_sequence=2, stop_id="S-004", arrival_delay=420, departure_delay=420),
                ],
            ),
        ]

        sample_alerts = [
            ServiceAlert(
                alert_id="ALT-01",
                header_text="Traffic Congestion on Michigan Ave",
                description_text="Buses on Route 1 experiencing delays up to 10 minutes due to downtown street maintenance.",
                cause="CONGESTION",
                effect="SIGNIFICANT_DELAYS",
                informed_entities=[{"agency_id": agency_id, "route_id": "1"}],
                agency_id=agency_id,
                timestamp=now,
            )
        ]

        feed = RealtimeFeedMessage(
            feed_timestamp=now,
            vehicles=sample_vehicles,
            trip_updates=sample_updates,
            alerts=sample_alerts,
        )
        return self.store.ingest_feed(feed)
