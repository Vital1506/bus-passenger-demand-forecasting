"""Zero-dependency, resilient Protocol Buffers wire-format and JSON parser for GTFS-Realtime."""

from __future__ import annotations

import io
import json
import struct
from typing import Any

from .models import (
    OccupancyStatus,
    RealtimeFeedMessage,
    ServiceAlert,
    StopTimeUpdate,
    TripUpdate,
    VehiclePosition,
    VehicleStatus,
)


def _read_varint(stream: io.BytesIO) -> int:
    result = 0
    shift = 0
    while True:
        b_raw = stream.read(1)
        if not b_raw:
            raise EOFError("Unexpected end of protobuf stream")
        b = b_raw[0]
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result


def _decode_raw_message(stream: io.BytesIO, length: int | None = None) -> dict[int, list[Any]]:
    """Decode a protobuf message stream into a dictionary of field_number -> [values]."""
    fields: dict[int, list[Any]] = {}
    end_pos = None
    if length is not None:
        end_pos = stream.tell() + length

    while True:
        if end_pos is not None and stream.tell() >= end_pos:
            break
        try:
            key = _read_varint(stream)
        except EOFError:
            break

        field_num = key >> 3
        wire_type = key & 0x07

        if wire_type == 0:  # Varint
            val = _read_varint(stream)
        elif wire_type == 1:  # 64-bit
            val = stream.read(8)
        elif wire_type == 2:  # Length-delimited
            v_len = _read_varint(stream)
            val = stream.read(v_len)
        elif wire_type == 5:  # 32-bit
            val = stream.read(4)
        else:
            # Unsupported or deprecated wire type (3, 4)
            break

        fields.setdefault(field_num, []).append((wire_type, val))

    return fields


def _get_string(fields: dict[int, list[Any]], field_num: int) -> str | None:
    entries = fields.get(field_num)
    if not entries:
        return None
    _, val = entries[0]
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8")
        except UnicodeDecodeError:
            return val.decode("latin1", errors="replace")
    return str(val)


def _get_int(fields: dict[int, list[Any]], field_num: int, default: int = 0) -> int:
    entries = fields.get(field_num)
    if not entries:
        return default
    _, val = entries[0]
    if isinstance(val, int):
        return val
    if isinstance(val, bytes):
        if len(val) == 4:
            return struct.unpack("<i", val)[0]
        if len(val) == 8:
            return struct.unpack("<q", val)[0]
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _get_float(fields: dict[int, list[Any]], field_num: int) -> float | None:
    entries = fields.get(field_num)
    if not entries:
        return None
    _, val = entries[0]
    if isinstance(val, bytes):
        if len(val) == 4:
            return struct.unpack("<f", val)[0]
        if len(val) == 8:
            return struct.unpack("<d", val)[0]
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _get_nested(fields: dict[int, list[Any]], field_num: int) -> dict[int, list[Any]] | None:
    entries = fields.get(field_num)
    if not entries:
        return None
    _, val = entries[0]
    if isinstance(val, bytes):
        return _decode_raw_message(io.BytesIO(val), len(val))
    return None


def _get_nested_list(fields: dict[int, list[Any]], field_num: int) -> list[dict[int, list[Any]]]:
    entries = fields.get(field_num, [])
    result = []
    for _, val in entries:
        if isinstance(val, bytes):
            result.append(_decode_raw_message(io.BytesIO(val), len(val)))
    return result


def parse_protobuf_feed(data: bytes, agency_id: str = "default") -> RealtimeFeedMessage:
    """Parse GTFS-RT binary Protocol Buffers into RealtimeFeedMessage."""
    stream = io.BytesIO(data)
    root = _decode_raw_message(stream, len(data))

    # Header is Tag 1
    header = _get_nested(root, 1) or {}
    gtfs_version = _get_string(header, 1) or "2.0"
    feed_timestamp = _get_int(header, 3, default=0)

    feed = RealtimeFeedMessage(
        feed_timestamp=feed_timestamp,
        gtfs_realtime_version=gtfs_version,
    )

    # Entities are repeated Tag 2
    entity_entries = _get_nested_list(root, 2)
    for entity in entity_entries:
        entity_id = _get_string(entity, 1) or "unknown"
        is_deleted = bool(_get_int(entity, 2, default=0))
        if is_deleted:
            continue

        # Tag 3: TripUpdate
        tu_data = _get_nested(entity, 3)
        if tu_data:
            trip_desc = _get_nested(tu_data, 1) or {}
            trip_id = _get_string(trip_desc, 1) or entity_id
            route_id = _get_string(trip_desc, 2)
            veh_desc = _get_nested(tu_data, 2) or {}
            vehicle_id = _get_string(veh_desc, 1)
            tu_timestamp = _get_int(tu_data, 4, default=feed_timestamp)
            delay = _get_int(tu_data, 5, default=0) if 5 in tu_data else None

            # Stop time updates (Tag 3 in TripUpdate)
            stu_entries = _get_nested_list(tu_data, 3)
            stop_time_updates = []
            for stu in stu_entries:
                seq = _get_int(stu, 1, default=0) if 1 in stu else None
                s_id = _get_string(stu, 2)
                arr_event = _get_nested(stu, 3) or {}
                dep_event = _get_nested(stu, 4) or {}

                arr_delay = _get_int(arr_event, 1, default=0) if 1 in arr_event else None
                arr_time = _get_int(arr_event, 2, default=0) if 2 in arr_event else None
                dep_delay = _get_int(dep_event, 1, default=0) if 1 in dep_event else None
                dep_time = _get_int(dep_event, 2, default=0) if 2 in dep_event else None

                stop_time_updates.append(
                    StopTimeUpdate(
                        stop_sequence=seq,
                        stop_id=s_id,
                        arrival_delay=arr_delay,
                        arrival_time=arr_time,
                        departure_delay=dep_delay,
                        departure_time=dep_time,
                    )
                )

            feed.trip_updates.append(
                TripUpdate(
                    trip_id=trip_id,
                    route_id=route_id,
                    vehicle_id=vehicle_id,
                    delay=delay,
                    timestamp=tu_timestamp,
                    stop_time_updates=stop_time_updates,
                    agency_id=agency_id,
                )
            )

        # Tag 4: VehiclePosition
        vp_data = _get_nested(entity, 4)
        if vp_data:
            trip_desc = _get_nested(vp_data, 1) or {}
            trip_id = _get_string(trip_desc, 1)
            route_id = _get_string(trip_desc, 2)

            pos_data = _get_nested(vp_data, 2) or {}
            lat = _get_float(pos_data, 1) or 0.0
            lon = _get_float(pos_data, 2) or 0.0
            bearing = _get_float(pos_data, 3)
            speed = _get_float(pos_data, 5)

            stop_seq = _get_int(vp_data, 3, default=0) if 3 in vp_data else None
            stop_id = _get_string(vp_data, 4)
            status_code = _get_int(vp_data, 5, default=2)
            vp_timestamp = _get_int(vp_data, 6, default=feed_timestamp)

            veh_desc = _get_nested(vp_data, 8) or {}
            vehicle_id = _get_string(veh_desc, 1) or entity_id

            occupancy_code = _get_int(vp_data, 9, default=7) if 9 in vp_data else None
            occ_pct = _get_int(vp_data, 10, default=0) if 10 in vp_data else None

            feed.vehicles.append(
                VehiclePosition(
                    vehicle_id=vehicle_id,
                    trip_id=trip_id,
                    route_id=route_id,
                    latitude=lat,
                    longitude=lon,
                    bearing=bearing,
                    speed_mps=speed,
                    current_status=VehicleStatus.from_code(status_code),
                    current_stop_sequence=stop_seq,
                    stop_id=stop_id,
                    occupancy_status=OccupancyStatus.from_code(occupancy_code),
                    occupancy_percentage=occ_pct,
                    timestamp=vp_timestamp,
                    agency_id=agency_id,
                )
            )

        # Tag 5: Alert
        alert_data = _get_nested(entity, 5)
        if alert_data:
            # Active period (Tag 1)
            active_periods = []
            for ap in _get_nested_list(alert_data, 1):
                start = _get_int(ap, 1, default=0) if 1 in ap else None
                end = _get_int(ap, 2, default=0) if 2 in ap else None
                active_periods.append((start, end))

            # Informed entity (Tag 5)
            informed_entities = []
            for ie in _get_nested_list(alert_data, 5):
                ie_dict = {}
                a_id = _get_string(ie, 1)
                r_id = _get_string(ie, 2)
                s_id = _get_string(ie, 4)
                if a_id:
                    ie_dict["agency_id"] = a_id
                if r_id:
                    ie_dict["route_id"] = r_id
                if s_id:
                    ie_dict["stop_id"] = s_id
                if ie_dict:
                    informed_entities.append(ie_dict)

            # Header text (Tag 10)
            h_container = _get_nested(alert_data, 10) or {}
            translations = _get_nested_list(h_container, 1)
            header_text = _get_string(translations[0], 1) if translations else "Service Alert"

            # Description (Tag 11)
            d_container = _get_nested(alert_data, 11) or {}
            d_translations = _get_nested_list(d_container, 1)
            desc_text = _get_string(d_translations[0], 1) if d_translations else None

            feed.alerts.append(
                ServiceAlert(
                    alert_id=entity_id,
                    header_text=header_text or "Service Alert",
                    description_text=desc_text,
                    active_periods=active_periods,
                    informed_entities=informed_entities,
                    agency_id=agency_id,
                    timestamp=feed_timestamp,
                )
            )

    return feed


def parse_json_feed(data: str | bytes | dict, agency_id: str = "default") -> RealtimeFeedMessage:
    """Parse GTFS-RT JSON format (supporting both camelCase and snake_case)."""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    if isinstance(data, str):
        payload = json.loads(data)
    else:
        payload = data

    header = payload.get("header") or {}
    feed_timestamp = int(header.get("timestamp") or header.get("feed_timestamp") or 0)
    version = str(header.get("gtfsRealtimeVersion") or header.get("gtfs_realtime_version") or "2.0")

    feed = RealtimeFeedMessage(
        feed_timestamp=feed_timestamp,
        gtfs_realtime_version=version,
    )

    entities = payload.get("entity") or payload.get("entities") or []
    for ent in entities:
        ent_id = str(ent.get("id", "unknown"))
        if ent.get("isDeleted") or ent.get("is_deleted"):
            continue

        # Vehicle
        vp = ent.get("vehicle") or ent.get("vehiclePosition") or ent.get("vehicle_position")
        if vp:
            trip = vp.get("trip") or {}
            pos = vp.get("position") or {}
            veh = vp.get("vehicle") or {}

            lat = float(pos.get("latitude") or 0.0)
            lon = float(pos.get("longitude") or 0.0)
            bearing = float(pos["bearing"]) if "bearing" in pos and pos["bearing"] is not None else None
            speed = float(pos["speed"]) if "speed" in pos and pos["speed"] is not None else None

            feed.vehicles.append(
                VehiclePosition(
                    vehicle_id=str(veh.get("id") or ent_id),
                    trip_id=str(trip.get("tripId") or trip.get("trip_id") or "") or None,
                    route_id=str(trip.get("routeId") or trip.get("route_id") or "") or None,
                    latitude=lat,
                    longitude=lon,
                    bearing=bearing,
                    speed_mps=speed,
                    current_status=VehicleStatus.from_code(vp.get("currentStatus") or vp.get("current_status")),
                    current_stop_sequence=vp.get("currentStopSequence") or vp.get("current_stop_sequence"),
                    stop_id=str(vp.get("stopId") or vp.get("stop_id") or "") or None,
                    occupancy_status=OccupancyStatus.from_code(vp.get("occupancyStatus") or vp.get("occupancy_status")),
                    occupancy_percentage=vp.get("occupancyPercentage") or vp.get("occupancy_percentage"),
                    timestamp=int(vp.get("timestamp") or feed_timestamp),
                    agency_id=agency_id,
                )
            )

        # TripUpdate
        tu = ent.get("tripUpdate") or ent.get("trip_update")
        if tu:
            trip = tu.get("trip") or {}
            veh = tu.get("vehicle") or {}
            stus = tu.get("stopTimeUpdate") or tu.get("stop_time_update") or tu.get("stop_time_updates") or []

            updates = []
            for s in stus:
                arr = s.get("arrival") or {}
                dep = s.get("departure") or {}
                updates.append(
                    StopTimeUpdate(
                        stop_sequence=s.get("stopSequence") or s.get("stop_sequence"),
                        stop_id=str(s.get("stopId") or s.get("stop_id") or "") or None,
                        arrival_delay=arr.get("delay"),
                        arrival_time=arr.get("time"),
                        departure_delay=dep.get("delay"),
                        departure_time=dep.get("time"),
                        schedule_relationship=str(s.get("scheduleRelationship") or s.get("schedule_relationship") or "SCHEDULED"),
                    )
                )

            feed.trip_updates.append(
                TripUpdate(
                    trip_id=str(trip.get("tripId") or trip.get("trip_id") or ent_id),
                    route_id=str(trip.get("routeId") or trip.get("route_id") or "") or None,
                    vehicle_id=str(veh.get("id") or veh.get("vehicle_id") or "") or None,
                    delay=int(tu["delay"]) if "delay" in tu and tu["delay"] is not None else None,
                    timestamp=int(tu.get("timestamp") or feed_timestamp),
                    stop_time_updates=updates,
                    agency_id=agency_id,
                )
            )

        # Alert
        al = ent.get("alert")
        if al:
            header_val = al.get("headerText") or al.get("header_text") or "Service Alert"
            if isinstance(header_val, dict):
                tr = header_val.get("translation") or header_val.get("translations") or []
                header_text = tr[0].get("text") if tr else "Service Alert"
            else:
                header_text = str(header_val)

            desc_val = al.get("descriptionText") or al.get("description_text")
            desc_text = None
            if isinstance(desc_val, dict):
                tr = desc_val.get("translation") or desc_val.get("translations") or []
                desc_text = tr[0].get("text") if tr else None
            elif desc_val is not None:
                desc_text = str(desc_val)

            feed.alerts.append(
                ServiceAlert(
                    alert_id=ent_id,
                    header_text=header_text,
                    description_text=desc_text,
                    cause=str(al.get("cause", "UNKNOWN_CAUSE")),
                    effect=str(al.get("effect", "UNKNOWN_EFFECT")),
                    agency_id=agency_id,
                    timestamp=feed_timestamp,
                )
            )

    return feed


def parse_realtime_feed(data: bytes | str | dict, agency_id: str = "default") -> RealtimeFeedMessage:
    """Automatically detect format (Protobuf vs JSON) and parse into RealtimeFeedMessage."""
    if isinstance(data, dict):
        return parse_json_feed(data, agency_id=agency_id)

    if isinstance(data, str):
        trimmed = data.strip()
        if trimmed.startswith("{") or trimmed.startswith("["):
            return parse_json_feed(trimmed, agency_id=agency_id)
        # Otherwise bytes encoded
        return parse_protobuf_feed(data.encode("utf-8"), agency_id=agency_id)

    if isinstance(data, (bytes, bytearray)):
        # Check if JSON
        try:
            trimmed = data.strip()
            if trimmed.startswith(b"{") or trimmed.startswith(b"["):
                return parse_json_feed(data, agency_id=agency_id)
        except Exception:
            pass
        return parse_protobuf_feed(data, agency_id=agency_id)

    raise ValueError("Unsupported data type for GTFS-RT feed: " + type(data).__name__)
