"""In-memory telemetry store, anomaly filtering, and spatial indexing for live GTFS-RT entities."""

from __future__ import annotations

import math
import threading
import time
from typing import Any

from .models import (
    OccupancyStatus,
    RealtimeFeedMessage,
    ServiceAlert,
    TripUpdate,
    VehiclePosition,
)

# Operational limits
MAX_SPEED_KMH = 130.0  # Teleportation threshold for urban transit
MAX_SPEED_MPS = MAX_SPEED_KMH / 3.6  # ~36.1 m/s
STALE_THRESHOLD_SECONDS = 600  # 10 minutes


def _haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in meters between two coordinates using Haversine formula."""
    R = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


class RealtimeTelemetryStore:
    """Thread-safe, validated in-memory store for live fleet positions, delays, and alerts."""

    def __init__(self, stale_timeout: int = STALE_THRESHOLD_SECONDS):
        self._lock = threading.Lock()
        self._stale_timeout = stale_timeout

        # Storage
        self._vehicles: dict[str, VehiclePosition] = {}  # vehicle_id -> VehiclePosition
        self._trip_updates: dict[str, TripUpdate] = {}  # trip_id -> TripUpdate
        self._alerts: dict[str, ServiceAlert] = {}  # alert_id -> ServiceAlert

        # Telemetry history for jump detection
        self._prev_positions: dict[str, tuple[float, float, int]] = {}  # vehicle_id -> (lat, lon, ts)

        # Ingestion metrics
        self._total_ingested_feeds = 0
        self._last_ingest_time: float | None = None
        self._discarded_jumps = 0
        self._discarded_invalid_coords = 0

    def ingest_feed(self, feed: RealtimeFeedMessage) -> dict[str, Any]:
        """Validate, filter, and merge feed entities into the store."""
        now = time.time()
        now_ts = int(now)
        accepted_vehicles = 0
        discarded_coords = 0
        discarded_jumps = 0

        with self._lock:
            # 1. Process vehicles
            for v in feed.vehicles:
                # Coordinate validity check
                if not (-90.0 <= v.latitude <= 90.0 and -180.0 <= v.longitude <= 180.0):
                    discarded_coords += 1
                    continue
                if abs(v.latitude) < 0.0001 and abs(v.longitude) < 0.0001:  # Null island
                    discarded_coords += 1
                    continue

                # Ensure timestamp
                v_ts = v.timestamp or feed.feed_timestamp or now_ts

                # Jump / teleportation detection
                prev = self._prev_positions.get(v.vehicle_id)
                if prev:
                    prev_lat, prev_lon, prev_ts = prev
                    time_diff = v_ts - prev_ts
                    if time_diff > 2:  # at least 2 seconds between updates
                        dist_m = _haversine_distance_meters(prev_lat, prev_lon, v.latitude, v.longitude)
                        calc_speed_mps = dist_m / time_diff
                        if calc_speed_mps > MAX_SPEED_MPS:
                            # Unrealistic jump!
                            discarded_jumps += 1
                            continue

                # Accept vehicle
                v.timestamp = v_ts
                self._vehicles[v.vehicle_id] = v
                self._prev_positions[v.vehicle_id] = (v.latitude, v.longitude, v_ts)
                accepted_vehicles += 1

            # 2. Process trip updates
            for tu in feed.trip_updates:
                tu_ts = tu.timestamp or feed.feed_timestamp or now_ts
                tu.timestamp = tu_ts
                self._trip_updates[tu.trip_id] = tu

            # 3. Process alerts
            for al in feed.alerts:
                al_ts = al.timestamp or feed.feed_timestamp or now_ts
                al.timestamp = al_ts
                self._alerts[al.alert_id] = al

            # 4. Prune stale vehicles
            cutoff = now_ts - self._stale_timeout
            stale_v_ids = [vid for vid, v in self._vehicles.items() if v.timestamp < cutoff]
            for vid in stale_v_ids:
                del self._vehicles[vid]
                self._prev_positions.pop(vid, None)

            # Prune stale trip updates
            stale_tu_ids = [tid for tid, tu in self._trip_updates.items() if tu.timestamp < cutoff]
            for tid in stale_tu_ids:
                del self._trip_updates[tid]

            self._total_ingested_feeds += 1
            self._last_ingest_time = now
            self._discarded_coords = discarded_coords
            self._discarded_jumps += discarded_jumps

        return {
            "accepted_vehicles": accepted_vehicles,
            "discarded_coords": discarded_coords,
            "discarded_jumps": discarded_jumps,
            "active_vehicles": len(self._vehicles),
            "active_trip_updates": len(self._trip_updates),
            "active_alerts": len(self._alerts),
        }

    def get_vehicles(
        self,
        route_id: str | None = None,
        min_lat: float | None = None,
        max_lat: float | None = None,
        min_lon: float | None = None,
        max_lon: float | None = None,
        agency_id: str | None = None,
    ) -> list[VehiclePosition]:
        """Query active vehicles with optional route, agency, and spatial bounding box filters."""
        with self._lock:
            vehicles = list(self._vehicles.values())

        filtered = []
        for v in vehicles:
            if agency_id and v.agency_id != agency_id:
                continue
            if route_id and str(v.route_id) != str(route_id):
                continue
            if min_lat is not None and v.latitude < min_lat:
                continue
            if max_lat is not None and v.latitude > max_lat:
                continue
            if min_lon is not None and v.longitude < min_lon:
                continue
            if max_lon is not None and v.longitude > max_lon:
                continue
            filtered.append(v)

        return filtered

    def get_vehicle(self, vehicle_id: str) -> VehiclePosition | None:
        """Get single vehicle by ID."""
        with self._lock:
            return self._vehicles.get(vehicle_id)

    def get_trip_updates(
        self,
        route_id: str | None = None,
        trip_id: str | None = None,
        agency_id: str | None = None,
    ) -> list[TripUpdate]:
        """Query trip updates with optional route or trip filter."""
        with self._lock:
            updates = list(self._trip_updates.values())

        filtered = []
        for tu in updates:
            if agency_id and tu.agency_id != agency_id:
                continue
            if route_id and str(tu.route_id) != str(route_id):
                continue
            if trip_id and str(tu.trip_id) != str(trip_id):
                continue
            filtered.append(tu)

        return filtered

    def get_alerts(
        self,
        route_id: str | None = None,
        stop_id: str | None = None,
        agency_id: str | None = None,
    ) -> list[ServiceAlert]:
        """Query active service alerts affecting a route, stop, or agency."""
        with self._lock:
            alerts = list(self._alerts.values())

        if not (route_id or stop_id or agency_id):
            return alerts

        filtered = []
        for al in alerts:
            if agency_id and al.agency_id == agency_id:
                filtered.append(al)
                continue
            # Check informed entities
            for ent in al.informed_entities:
                if route_id and ent.get("route_id") == str(route_id):
                    filtered.append(al)
                    break
                if stop_id and ent.get("stop_id") == str(stop_id):
                    filtered.append(al)
                    break

        return filtered

    def get_fleet_summary(self, agency_id: str | None = None) -> dict[str, Any]:
        """Aggregate fleet performance KPIs (active count, occupancy distribution, on-time rate)."""
        with self._lock:
            vehicles = list(self._vehicles.values())
            trip_updates = list(self._trip_updates.values())

        if agency_id:
            vehicles = [v for v in vehicles if v.agency_id == agency_id]
            trip_updates = [t for t in trip_updates if t.agency_id == agency_id]

        total_vehicles = len(vehicles)
        occupancy_dist: dict[str, int] = {}
        for v in vehicles:
            status_val = v.occupancy_status.value
            occupancy_dist[status_val] = occupancy_dist.get(status_val, 0) + 1

        # Delay statistics
        delays = [t.delay for t in trip_updates if t.delay is not None]
        on_time_count = 0  # between -60s (early) and 300s (5m late)
        delayed_count = 0  # > 300s late
        early_count = 0    # < -60s early

        for d in delays:
            if d < -60:
                early_count += 1
            elif d > 300:
                delayed_count += 1
            else:
                on_time_count += 1

        on_time_rate = round(on_time_count / len(delays) * 100, 1) if delays else None
        avg_delay_sec = round(sum(delays) / len(delays), 1) if delays else 0.0

        return {
            "total_active_vehicles": total_vehicles,
            "total_tracked_trips": len(trip_updates),
            "on_time_performance_pct": on_time_rate,
            "average_delay_seconds": avg_delay_sec,
            "delay_breakdown": {
                "on_time": on_time_count,
                "delayed": delayed_count,
                "early": early_count,
            },
            "occupancy_distribution": occupancy_dist,
            "last_ingest_time": self._last_ingest_time,
        }

    def clear(self) -> None:
        """Clear all active entities (used for testing)."""
        with self._lock:
            self._vehicles.clear()
            self._trip_updates.clear()
            self._alerts.clear()
            self._prev_positions.clear()
