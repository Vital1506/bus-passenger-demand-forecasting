"""Data models for GTFS-Realtime entities according to the official GTFS-RT specification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class OccupancyStatus(str, Enum):
    EMPTY = "EMPTY"
    MANY_SEATS_AVAILABLE = "MANY_SEATS_AVAILABLE"
    FEW_SEATS_AVAILABLE = "FEW_SEATS_AVAILABLE"
    STANDING_ROOM_ONLY = "STANDING_ROOM_ONLY"
    CRUSHED_STANDING_ROOM_ONLY = "CRUSHED_STANDING_ROOM_ONLY"
    FULL = "FULL"
    NOT_ACCEPTING_PASSENGERS = "NOT_ACCEPTING_PASSENGERS"
    NO_DATA_AVAILABLE = "NO_DATA_AVAILABLE"
    NOT_BOARDABLE = "NOT_BOARDABLE"

    @classmethod
    def from_code(cls, code: int | str | None) -> OccupancyStatus:
        if code is None:
            return cls.NO_DATA_AVAILABLE
        if isinstance(code, str):
            clean = code.strip().upper()
            try:
                return cls[clean]
            except KeyError:
                return cls.NO_DATA_AVAILABLE
        # Standard GTFS-RT integer mapping
        mapping = {
            0: cls.EMPTY,
            1: cls.MANY_SEATS_AVAILABLE,
            2: cls.FEW_SEATS_AVAILABLE,
            3: cls.STANDING_ROOM_ONLY,
            4: cls.CRUSHED_STANDING_ROOM_ONLY,
            5: cls.FULL,
            6: cls.NOT_ACCEPTING_PASSENGERS,
            7: cls.NO_DATA_AVAILABLE,
            8: cls.NOT_BOARDABLE,
        }
        return mapping.get(int(code), cls.NO_DATA_AVAILABLE)


class VehicleStatus(str, Enum):
    INCOMING_AT = "INCOMING_AT"
    STOPPED_AT = "STOPPED_AT"
    IN_TRANSIT_TO = "IN_TRANSIT_TO"

    @classmethod
    def from_code(cls, code: int | str | None) -> VehicleStatus:
        if isinstance(code, str):
            clean = code.strip().upper()
            try:
                return cls[clean]
            except KeyError:
                return cls.IN_TRANSIT_TO
        mapping = {
            0: cls.INCOMING_AT,
            1: cls.STOPPED_AT,
            2: cls.IN_TRANSIT_TO,
        }
        return mapping.get(int(code or 2), cls.IN_TRANSIT_TO)


@dataclass
class VehiclePosition:
    vehicle_id: str
    trip_id: str | None = None
    route_id: str | None = None
    latitude: float = 0.0
    longitude: float = 0.0
    bearing: float | None = None
    speed_mps: float | None = None  # meters per second
    current_status: VehicleStatus = VehicleStatus.IN_TRANSIT_TO
    current_stop_sequence: int | None = None
    stop_id: str | None = None
    occupancy_status: OccupancyStatus = OccupancyStatus.NO_DATA_AVAILABLE
    occupancy_percentage: int | None = None
    timestamp: int = 0  # POSIX epoch seconds
    agency_id: str = "default"

    @property
    def speed_kmh(self) -> float | None:
        return self.speed_mps * 3.6 if self.speed_mps is not None else None

    @property
    def iso_timestamp(self) -> str:
        if not self.timestamp:
            return datetime.now(timezone.utc).isoformat()
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "trip_id": self.trip_id,
            "route_id": self.route_id,
            "latitude": round(self.latitude, 6),
            "longitude": round(self.longitude, 6),
            "bearing": round(self.bearing, 1) if self.bearing is not None else None,
            "speed_kmh": round(self.speed_kmh, 1) if self.speed_kmh is not None else None,
            "speed_mps": round(self.speed_mps, 2) if self.speed_mps is not None else None,
            "current_status": self.current_status.value,
            "current_stop_sequence": self.current_stop_sequence,
            "stop_id": self.stop_id,
            "occupancy_status": self.occupancy_status.value,
            "occupancy_percentage": self.occupancy_percentage,
            "timestamp": self.timestamp,
            "iso_timestamp": self.iso_timestamp,
            "agency_id": self.agency_id,
        }


@dataclass
class StopTimeUpdate:
    stop_sequence: int | None = None
    stop_id: str | None = None
    arrival_delay: int | None = None  # delay in seconds
    arrival_time: int | None = None  # POSIX epoch seconds
    departure_delay: int | None = None  # delay in seconds
    departure_time: int | None = None  # POSIX epoch seconds
    schedule_relationship: str = "SCHEDULED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_sequence": self.stop_sequence,
            "stop_id": self.stop_id,
            "arrival_delay": self.arrival_delay,
            "arrival_time": self.arrival_time,
            "departure_delay": self.departure_delay,
            "departure_time": self.departure_time,
            "schedule_relationship": self.schedule_relationship,
        }


@dataclass
class TripUpdate:
    trip_id: str
    route_id: str | None = None
    vehicle_id: str | None = None
    delay: int | None = None  # delay in seconds (positive=late, negative=early)
    timestamp: int = 0
    stop_time_updates: list[StopTimeUpdate] = field(default_factory=list)
    schedule_relationship: str = "SCHEDULED"
    agency_id: str = "default"

    @property
    def delay_minutes(self) -> float | None:
        return round(self.delay / 60.0, 1) if self.delay is not None else None

    @property
    def iso_timestamp(self) -> str:
        if not self.timestamp:
            return datetime.now(timezone.utc).isoformat()
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "trip_id": self.trip_id,
            "route_id": self.route_id,
            "vehicle_id": self.vehicle_id,
            "delay": self.delay,
            "delay_minutes": self.delay_minutes,
            "timestamp": self.timestamp,
            "iso_timestamp": self.iso_timestamp,
            "schedule_relationship": self.schedule_relationship,
            "agency_id": self.agency_id,
            "stop_time_updates": [u.to_dict() for u in self.stop_time_updates],
        }


@dataclass
class ServiceAlert:
    alert_id: str
    header_text: str
    description_text: str | None = None
    cause: str = "UNKNOWN_CAUSE"
    effect: str = "UNKNOWN_EFFECT"
    active_periods: list[tuple[int | None, int | None]] = field(default_factory=list)
    informed_entities: list[dict[str, str]] = field(default_factory=list)  # list of {agency_id, route_id, stop_id}
    agency_id: str = "default"
    timestamp: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "header_text": self.header_text,
            "description_text": self.description_text,
            "cause": self.cause,
            "effect": self.effect,
            "active_periods": [{"start": p[0], "end": p[1]} for p in self.active_periods],
            "informed_entities": self.informed_entities,
            "agency_id": self.agency_id,
            "timestamp": self.timestamp,
        }


@dataclass
class RealtimeFeedMessage:
    feed_timestamp: int
    gtfs_realtime_version: str = "2.0"
    vehicles: list[VehiclePosition] = field(default_factory=list)
    trip_updates: list[TripUpdate] = field(default_factory=list)
    alerts: list[ServiceAlert] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feed_timestamp": self.feed_timestamp,
            "gtfs_realtime_version": self.gtfs_realtime_version,
            "counts": {
                "vehicles": len(self.vehicles),
                "trip_updates": len(self.trip_updates),
                "alerts": len(self.alerts),
            },
            "warnings": self.warnings[:20],
        }
