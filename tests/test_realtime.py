"""Comprehensive unit tests for GTFS-Realtime Protobuf and JSON parser, telemetry validation, and spatial index."""

from __future__ import annotations

import io
import json
import struct
import time
import pytest

from src.realtime.models import (
    OccupancyStatus,
    RealtimeFeedMessage,
    ServiceAlert,
    StopTimeUpdate,
    TripUpdate,
    VehiclePosition,
    VehicleStatus,
)
from src.realtime.protobuf_parser import (
    parse_json_feed,
    parse_protobuf_feed,
    parse_realtime_feed,
)
from src.realtime.feed_ingester import RealtimeTelemetryStore
from src.realtime.service import RealtimeService


def _encode_varint(value: int) -> bytes:
    bits = value & 0x7F
    value >>= 7
    buf = bytearray()
    while value:
        buf.append(0x80 | bits)
        bits = value & 0x7F
        value >>= 7
    buf.append(bits)
    return bytes(buf)


def _encode_field(field_number: int, wire_type: int, payload: bytes) -> bytes:
    key = (field_number << 3) | wire_type
    return _encode_varint(key) + payload


def _encode_varint_field(field_number: int, value: int) -> bytes:
    return _encode_field(field_number, 0, _encode_varint(value))


def _encode_string_field(field_number: int, text: str) -> bytes:
    raw = text.encode("utf-8")
    return _encode_field(field_number, 2, _encode_varint(len(raw)) + raw)


def _encode_float_field(field_number: int, val: float) -> bytes:
    raw = struct.pack("<f", val)
    return _encode_field(field_number, 5, raw)


def _encode_message_field(field_number: int, msg_bytes: bytes) -> bytes:
    return _encode_field(field_number, 2, _encode_varint(len(msg_bytes)) + msg_bytes)


def create_synthetic_gtfs_rt_protobuf() -> bytes:
    """Build a valid binary Protobuf FeedMessage according to GTFS-RT spec."""
    now = int(time.time())

    # Header: Tag 1 (gtfs_version="2.0"), Tag 3 (timestamp)
    header_bytes = _encode_string_field(1, "2.0") + _encode_varint_field(3, now)
    header = _encode_message_field(1, header_bytes)

    # Vehicle 1:
    trip_desc = _encode_string_field(1, "TRIP-01") + _encode_string_field(2, "ROUTE-10")
    pos_desc = _encode_float_field(1, 41.8781) + _encode_float_field(2, -87.6298) + _encode_float_field(3, 90.0) + _encode_float_field(5, 12.5)
    veh_desc = _encode_string_field(1, "BUS-101")
    vp_bytes = (
        _encode_message_field(1, trip_desc)
        + _encode_message_field(2, pos_desc)
        + _encode_varint_field(3, 5)  # stop seq
        + _encode_string_field(4, "STOP-50")
        + _encode_varint_field(5, 1)  # status STOPPED_AT
        + _encode_varint_field(6, now)
        + _encode_message_field(8, veh_desc)
        + _encode_varint_field(9, 1)  # occupancy MANY_SEATS_AVAILABLE
    )
    entity_1 = _encode_string_field(1, "ENT-01") + _encode_message_field(4, vp_bytes)

    # TripUpdate 1:
    stu = (
        _encode_varint_field(1, 5)
        + _encode_string_field(2, "STOP-50")
        + _encode_message_field(3, _encode_varint_field(1, 150))  # arrival delay +150s
    )
    tu_bytes = (
        _encode_message_field(1, trip_desc)
        + _encode_message_field(2, veh_desc)
        + _encode_message_field(3, stu)
        + _encode_varint_field(4, now)
        + _encode_varint_field(5, 150)
    )
    entity_2 = _encode_string_field(1, "ENT-02") + _encode_message_field(3, tu_bytes)

    return header + _encode_message_field(2, entity_1) + _encode_message_field(2, entity_2)


def test_protobuf_wire_parser():
    raw_pb = create_synthetic_gtfs_rt_protobuf()
    feed = parse_protobuf_feed(raw_pb, agency_id="CTA")

    assert feed.gtfs_realtime_version == "2.0"
    assert len(feed.vehicles) == 1
    assert len(feed.trip_updates) == 1

    v = feed.vehicles[0]
    assert v.vehicle_id == "BUS-101"
    assert v.route_id == "ROUTE-10"
    assert v.trip_id == "TRIP-01"
    assert pytest.approx(v.latitude, 0.001) == 41.8781
    assert pytest.approx(v.longitude, 0.001) == -87.6298
    assert v.current_status == VehicleStatus.STOPPED_AT
    assert v.occupancy_status == OccupancyStatus.MANY_SEATS_AVAILABLE

    tu = feed.trip_updates[0]
    assert tu.trip_id == "TRIP-01"
    assert tu.route_id == "ROUTE-10"
    assert tu.delay == 150
    assert tu.delay_minutes == 2.5
    assert len(tu.stop_time_updates) == 1
    assert tu.stop_time_updates[0].arrival_delay == 150


def test_json_gtfs_rt_parser():
    now = int(time.time())
    payload = {
        "header": {"gtfsRealtimeVersion": "2.0", "timestamp": now},
        "entity": [
            {
                "id": "v-202",
                "vehicle": {
                    "trip": {"tripId": "TRIP-99", "routeId": "ROUTE-20"},
                    "position": {"latitude": 41.85, "longitude": -87.65, "bearing": 180.0, "speed": 10.0},
                    "timestamp": now,
                    "currentStatus": "IN_TRANSIT_TO",
                    "occupancyStatus": "FULL",
                    "vehicle": {"id": "BUS-202"}
                }
            },
            {
                "id": "tu-99",
                "tripUpdate": {
                    "trip": {"tripId": "TRIP-99", "routeId": "ROUTE-20"},
                    "delay": -30,
                    "timestamp": now,
                    "stopTimeUpdate": [
                        {"stopSequence": 1, "stopId": "S1", "arrival": {"delay": -30}}
                    ]
                }
            },
            {
                "id": "alt-01",
                "alert": {
                    "headerText": "Detour on Route 20",
                    "cause": "CONSTRUCTION",
                    "effect": "DETOUR"
                }
            }
        ]
    }

    feed = parse_json_feed(payload, agency_id="CTA")
    assert len(feed.vehicles) == 1
    assert feed.vehicles[0].vehicle_id == "BUS-202"
    assert feed.vehicles[0].occupancy_status == OccupancyStatus.FULL
    assert feed.vehicles[0].speed_kmh == 36.0

    assert len(feed.trip_updates) == 1
    assert feed.trip_updates[0].delay == -30

    assert len(feed.alerts) == 1
    assert feed.alerts[0].header_text == "Detour on Route 20"
    assert feed.alerts[0].cause == "CONSTRUCTION"


def test_telemetry_store_validation_and_filtering():
    store = RealtimeTelemetryStore(stale_timeout=300)
    now = int(time.time())

    # 1. Normal vehicle
    v1 = VehiclePosition(
        vehicle_id="V1",
        route_id="R1",
        latitude=41.88,
        longitude=-87.63,
        timestamp=now,
    )
    # 2. Invalid coordinates (Null Island)
    v_null = VehiclePosition(
        vehicle_id="V_NULL",
        latitude=0.0,
        longitude=0.0,
        timestamp=now,
    )
    # 3. Invalid out-of-range coordinates
    v_bad = VehiclePosition(
        vehicle_id="V_BAD",
        latitude=120.0,
        longitude=-87.63,
        timestamp=now,
    )

    feed = RealtimeFeedMessage(feed_timestamp=now, vehicles=[v1, v_null, v_bad])
    res = store.ingest_feed(feed)

    assert res["accepted_vehicles"] == 1
    assert res["discarded_coords"] == 2
    assert len(store.get_vehicles()) == 1

    # 4. GPS Jump detection: move V1 by 50 km in 5 seconds (> 36,000 km/h)
    v1_jump = VehiclePosition(
        vehicle_id="V1",
        route_id="R1",
        latitude=42.50,  # ~70 km north
        longitude=-87.63,
        timestamp=now + 5,
    )
    jump_feed = RealtimeFeedMessage(feed_timestamp=now + 5, vehicles=[v1_jump])
    res_jump = store.ingest_feed(jump_feed)

    assert res_jump["discarded_jumps"] == 1
    # V1 position should remain at original location
    current_v1 = store.get_vehicle("V1")
    assert pytest.approx(current_v1.latitude, 0.001) == 41.88


def test_spatial_query_and_fleet_summary():
    store = RealtimeTelemetryStore()
    now = int(time.time())

    store.ingest_feed(RealtimeFeedMessage(
        feed_timestamp=now,
        vehicles=[
            VehiclePosition(vehicle_id="VA", route_id="10", latitude=41.80, longitude=-87.60, timestamp=now, occupancy_status=OccupancyStatus.EMPTY),
            VehiclePosition(vehicle_id="VB", route_id="10", latitude=41.85, longitude=-87.62, timestamp=now, occupancy_status=OccupancyStatus.MANY_SEATS_AVAILABLE),
            VehiclePosition(vehicle_id="VC", route_id="20", latitude=41.95, longitude=-87.70, timestamp=now, occupancy_status=OccupancyStatus.FULL),
        ],
        trip_updates=[
            TripUpdate(trip_id="T1", route_id="10", delay=100, timestamp=now),
            TripUpdate(trip_id="T2", route_id="10", delay=450, timestamp=now),  # Delayed
        ]
    ))

    # Route filter
    r10_vehs = store.get_vehicles(route_id="10")
    assert len(r10_vehs) == 2

    # Bounding box filter (south of 41.90)
    south_vehs = store.get_vehicles(max_lat=41.90)
    assert len(south_vehs) == 2

    # Fleet summary
    summary = store.get_fleet_summary()
    assert summary["total_active_vehicles"] == 3
    assert summary["total_tracked_trips"] == 2
    assert summary["occupancy_distribution"]["EMPTY"] == 1
    assert summary["occupancy_distribution"]["FULL"] == 1
    assert summary["delay_breakdown"]["delayed"] == 1
    assert summary["delay_breakdown"]["on_time"] == 1


def test_realtime_service_synthetic_snapshot():
    service = RealtimeService(RealtimeTelemetryStore())
    res = service.generate_synthetic_live_snapshot(agency_id="CTA")

    assert res["active_vehicles"] == 3
    assert res["active_trip_updates"] == 3
    assert res["active_alerts"] == 1

    vehicles = service.get_vehicles()
    assert len(vehicles) == 3
    assert any(v["vehicle_id"] == "CTA-4102" for v in vehicles)

    summary = service.get_summary()
    assert summary["total_active_vehicles"] == 3
    assert summary["on_time_performance_pct"] is not None


def test_realtime_api_endpoints():
    import io
    from starlette.datastructures import UploadFile
    from fastapi import HTTPException
    import src.app as app_mod

    # 1. Routes registered
    paths = {r.path for r in app_mod.app.routes}
    assert "/api/realtime/vehicles" in paths
    assert "/api/realtime/vehicles/{vehicle_id}" in paths
    assert "/api/realtime/trip-updates" in paths
    assert "/api/realtime/alerts" in paths
    assert "/api/realtime/summary" in paths
    assert "/api/realtime/ingest" in paths
    assert "/api/realtime/demo-snapshot" in paths

    # 2. Trigger demo snapshot
    demo_res = app_mod.generate_realtime_demo_snapshot(agency_id="CTA")
    assert demo_res["stats"]["accepted_vehicles"] == 3

    # 3. Query vehicles
    vehs = app_mod.get_realtime_vehicles()
    assert len(vehs) >= 3

    v_details = app_mod.get_realtime_vehicle("CTA-4102")
    assert v_details["vehicle_id"] == "CTA-4102"
    assert v_details["latitude"] > 40.0

    # 4. Query trip updates
    tus = app_mod.get_realtime_trip_updates(route_id="1")
    assert len(tus) >= 1

    # 5. Query alerts
    alerts = app_mod.get_realtime_alerts(route_id="1")
    assert len(alerts) >= 1

    # 6. Summary
    summary = app_mod.get_realtime_summary()
    assert summary["total_active_vehicles"] >= 3

    # 7. Ingest binary upload via API
    pb_bytes = create_synthetic_gtfs_rt_protobuf()
    up_file = UploadFile(file=io.BytesIO(pb_bytes), filename="feed.pb")
    ingest_res = app_mod.ingest_realtime_feed(file=up_file, agency_id="CTA")
    assert ingest_res["message"] == "GTFS-RT payload ingested successfully"
    assert ingest_res["stats"]["accepted_vehicles"] >= 1

