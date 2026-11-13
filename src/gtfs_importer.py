"""Validated, transactional ingestion engine for GTFS Schedule ZIP archives.

Adheres strictly to the General Transit Feed Specification (GTFS):
- agency.txt (required)
- routes.txt (required)
- stops.txt (required)
- trips.txt (required)
- stop_times.txt (required)
- calendar.txt / calendar_dates.txt (at least one required)
- shapes.txt (optional)

Security & Integrity:
- Safe ZIP decompression (Zip-Slip prevention)
- Maximum upload size enforcement
- Foreign key and relational integrity validation
- Coordinates range validation (-90 <= lat <= 90, -180 <= lon <= 180)
- GTFS extended time handling (e.g. 25:30:00 for trips past midnight)
- Atomic database transactions with rollback on failure
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db.models import (
    GTFSAgency,
    GTFSCalendar,
    GTFSCalendarDate,
    GTFSFeed,
    GTFSRoute,
    GTFSShape,
    GTFSStop,
    GTFSStopTime,
    GTFSTrip,
)

MAX_GTFS_ZIP_BYTES = 50 * 1024 * 1024  # 50 MB limit
_TIME_REGEX = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")


class GTFSValidationError(ValueError):
    """Raised when an uploaded GTFS feed is malformed or invalid."""


@dataclass
class GTFSImportSummary:
    feed_id: str
    agency_id: str
    sha256: str
    imported_at_utc: str
    agencies_count: int = 0
    routes_count: int = 0
    stops_count: int = 0
    trips_count: int = 0
    stop_times_count: int = 0
    calendar_count: int = 0
    calendar_dates_count: int = 0
    shapes_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feed_id": self.feed_id,
            "agency_id": self.agency_id,
            "sha256": self.sha256,
            "imported_at_utc": self.imported_at_utc,
            "counts": {
                "agencies": self.agencies_count,
                "routes": self.routes_count,
                "stops": self.stops_count,
                "trips": self.trips_count,
                "stop_times": self.stop_times_count,
                "calendar": self.calendar_count,
                "calendar_dates": self.calendar_dates_count,
                "shapes": self.shapes_count,
            },
            "warnings": self.warnings[:20],
        }


def _read_csv_from_zip(zf: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
    """Read a CSV file inside the ZIP, safely ignoring UTF-8 BOM."""
    # Handle files in root or single subfolder
    matched = None
    for name in zf.namelist():
        if Path(name).name.lower() == filename.lower():
            # Check ZipSlip: no '..' or absolute path
            if ".." in name or name.startswith("/") or name.startswith("\\"):
                raise GTFSValidationError(f"Insecure path detected in ZIP: {name}")
            matched = name
            break

    if not matched:
        return []

    try:
        raw = zf.read(matched)
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GTFSValidationError(f"{filename} must be UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise GTFSValidationError(f"{filename} has no header row")

    # Clean fieldnames (strip whitespace)
    clean_fieldnames = [f.strip() if f else "" for f in reader.fieldnames]
    reader.fieldnames = clean_fieldnames

    rows = []
    for r in reader:
        # Strip string values
        clean_row = {k.strip(): v.strip() for k, v in r.items() if k}
        rows.append(clean_row)
    return rows


def _parse_gtfs_date(val: str, filename: str, fieldname: str) -> date:
    """Parse YYYYMMDD date."""
    clean = val.strip()
    if len(clean) != 8 or not clean.isdigit():
        raise GTFSValidationError(f"{filename}: {fieldname} must be in YYYYMMDD format, found '{val}'")
    try:
        return date(int(clean[:4]), int(clean[4:6]), int(clean[6:8]))
    except ValueError as exc:
        raise GTFSValidationError(f"{filename}: invalid date value '{val}'") from exc


def _validate_time(val: str, filename: str, fieldname: str) -> str:
    """Check GTFS time string HH:MM:SS (hours can exceed 23 for service after midnight)."""
    clean = val.strip()
    match = _TIME_REGEX.match(clean)
    if not match:
        raise GTFSValidationError(f"{filename}: {fieldname} must be in HH:MM:SS format, found '{val}'")
    return clean


def parse_and_validate_gtfs(
    zip_bytes: bytes,
    feed_id: str | None = None,
    agency_id_override: str | None = None,
) -> tuple[dict[str, Any], GTFSImportSummary]:
    """Parse, cross-validate, and structure all GTFS entities from raw ZIP bytes."""
    if len(zip_bytes) > MAX_GTFS_ZIP_BYTES:
        raise GTFSValidationError(f"GTFS ZIP exceeds maximum allowed size of {MAX_GTFS_ZIP_BYTES // (1024*1024)} MB")

    digest = hashlib.sha256(zip_bytes).hexdigest()
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise GTFSValidationError("The uploaded file is not a valid ZIP archive") from exc

    # Check for ZipSlip / insecure paths across all entries in the archive
    for name in zf.namelist():
        if ".." in name or name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
            raise GTFSValidationError(f"Insecure path detected in ZIP: {name}")

    # 1. Check required files
    required_files = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt"]
    file_names = {Path(n).name.lower() for n in zf.namelist()}
    missing = [f for f in required_files if f not in file_names]
    if missing:
        raise GTFSValidationError(f"Missing required GTFS files: {', '.join(missing)}")

    has_calendar = "calendar.txt" in file_names
    has_calendar_dates = "calendar_dates.txt" in file_names
    if not (has_calendar or has_calendar_dates):
        raise GTFSValidationError("GTFS must contain at least one of calendar.txt or calendar_dates.txt")

    # 2. Parse Agency
    agency_rows = _read_csv_from_zip(zf, "agency.txt")
    if not agency_rows:
        raise GTFSValidationError("agency.txt contains no records")

    agencies = []
    primary_agency_id = None
    for row in agency_rows:
        a_id = row.get("agency_id") or "default_agency"
        a_name = row.get("agency_name")
        a_url = row.get("agency_url", "http://transit.example.com")
        a_tz = row.get("agency_timezone")
        if not a_name:
            raise GTFSValidationError("agency.txt: agency_name is required")
        if not a_tz:
            raise GTFSValidationError("agency.txt: agency_timezone is required")

        if not primary_agency_id:
            primary_agency_id = a_id

        agencies.append({
            "agency_id": a_id,
            "agency_name": a_name,
            "agency_url": a_url,
            "agency_timezone": a_tz,
            "agency_lang": row.get("agency_lang"),
            "agency_phone": row.get("agency_phone"),
        })

    effective_agency_id = agency_id_override or primary_agency_id or "default"
    effective_feed_id = feed_id or f"{effective_agency_id}_{digest[:12]}"
    summary = GTFSImportSummary(
        feed_id=effective_feed_id,
        agency_id=effective_agency_id,
        sha256=digest,
        imported_at_utc=datetime.now(timezone.utc).isoformat(),
        agencies_count=len(agencies),
    )

    agency_ids = {a["agency_id"] for a in agencies}

    # 3. Parse Stops
    stop_rows = _read_csv_from_zip(zf, "stops.txt")
    if not stop_rows:
        raise GTFSValidationError("stops.txt contains no records")

    stops = []
    stop_ids = set()
    for r in stop_rows:
        s_id = r.get("stop_id")
        if not s_id:
            raise GTFSValidationError("stops.txt: stop_id cannot be blank")
        s_name = r.get("stop_name", s_id)
        try:
            s_lat = float(r.get("stop_lat", 0))
            s_lon = float(r.get("stop_lon", 0))
        except (ValueError, TypeError) as exc:
            raise GTFSValidationError(f"stops.txt: invalid coordinates for stop '{s_id}'") from exc

        if not (-90.0 <= s_lat <= 90.0 and -180.0 <= s_lon <= 180.0):
            raise GTFSValidationError(f"stops.txt: coordinates out of range for stop '{s_id}': ({s_lat}, {s_lon})")

        stop_ids.add(s_id)
        stops.append({
            "stop_id": s_id,
            "stop_code": r.get("stop_code"),
            "stop_name": s_name,
            "stop_lat": s_lat,
            "stop_lon": s_lon,
            "zone_id": r.get("zone_id"),
            "location_type": int(r.get("location_type") or 0),
        })
    summary.stops_count = len(stops)

    # 4. Parse Routes
    route_rows = _read_csv_from_zip(zf, "routes.txt")
    if not route_rows:
        raise GTFSValidationError("routes.txt contains no records")

    routes = []
    route_ids = set()
    for r in route_rows:
        r_id = r.get("route_id")
        if not r_id:
            raise GTFSValidationError("routes.txt: route_id cannot be blank")
        short_name = r.get("route_short_name")
        long_name = r.get("route_long_name")
        if not (short_name or long_name):
            raise GTFSValidationError(f"routes.txt: route '{r_id}' requires at least route_short_name or route_long_name")

        r_type = int(r.get("route_type", 3))  # 3 = Bus in GTFS
        r_agency = r.get("agency_id") or primary_agency_id

        route_ids.add(r_id)
        routes.append({
            "route_id": r_id,
            "agency_id": r_agency,
            "route_short_name": short_name,
            "route_long_name": long_name,
            "route_type": r_type,
            "route_color": r.get("route_color"),
            "route_text_color": r.get("route_text_color"),
        })
    summary.routes_count = len(routes)

    # 5. Parse Calendar & Calendar Dates
    service_ids = set()
    calendars = []
    if has_calendar:
        for r in _read_csv_from_zip(zf, "calendar.txt"):
            s_id = r.get("service_id")
            if not s_id:
                continue
            service_ids.add(s_id)
            calendars.append({
                "service_id": s_id,
                "monday": int(r.get("monday") or 0),
                "tuesday": int(r.get("tuesday") or 0),
                "wednesday": int(r.get("wednesday") or 0),
                "thursday": int(r.get("thursday") or 0),
                "friday": int(r.get("friday") or 0),
                "saturday": int(r.get("saturday") or 0),
                "sunday": int(r.get("sunday") or 0),
                "start_date": _parse_gtfs_date(r["start_date"], "calendar.txt", "start_date"),
                "end_date": _parse_gtfs_date(r["end_date"], "calendar.txt", "end_date"),
            })
    summary.calendar_count = len(calendars)

    calendar_dates = []
    if has_calendar_dates:
        for r in _read_csv_from_zip(zf, "calendar_dates.txt"):
            s_id = r.get("service_id")
            if not s_id:
                continue
            service_ids.add(s_id)
            calendar_dates.append({
                "service_id": s_id,
                "date": _parse_gtfs_date(r["date"], "calendar_dates.txt", "date"),
                "exception_type": int(r.get("exception_type") or 1),
            })
    summary.calendar_dates_count = len(calendar_dates)

    # 6. Parse Trips (Validate route_id and service_id relationships)
    trip_rows = _read_csv_from_zip(zf, "trips.txt")
    if not trip_rows:
        raise GTFSValidationError("trips.txt contains no records")

    trips = []
    trip_ids = set()
    for r in trip_rows:
        t_id = r.get("trip_id")
        r_id = r.get("route_id")
        s_id = r.get("service_id")
        if not t_id:
            raise GTFSValidationError("trips.txt: trip_id cannot be blank")
        if r_id not in route_ids:
            raise GTFSValidationError(f"trips.txt: trip '{t_id}' references unknown route_id '{r_id}'")
        if s_id not in service_ids:
            summary.warnings.append(f"Trip '{t_id}' references service_id '{s_id}' absent from calendar files")

        dir_id = None
        if r.get("direction_id") in ("0", "1"):
            dir_id = int(r["direction_id"])

        trip_ids.add(t_id)
        trips.append({
            "trip_id": t_id,
            "route_id": r_id,
            "service_id": s_id,
            "trip_headsign": r.get("trip_headsign"),
            "direction_id": dir_id,
            "shape_id": r.get("shape_id"),
        })
    summary.trips_count = len(trips)

    # 7. Parse Stop Times (Validate trip_id and stop_id relationships)
    stop_time_rows = _read_csv_from_zip(zf, "stop_times.txt")
    if not stop_time_rows:
        raise GTFSValidationError("stop_times.txt contains no records")

    stop_times = []
    for r in stop_time_rows:
        t_id = r.get("trip_id")
        s_id = r.get("stop_id")
        seq_str = r.get("stop_sequence")
        arr_time = r.get("arrival_time")
        dep_time = r.get("departure_time")

        if not (t_id and s_id and seq_str and arr_time and dep_time):
            raise GTFSValidationError("stop_times.txt: trip_id, stop_id, stop_sequence, arrival_time, departure_time are all required")

        if t_id not in trip_ids:
            raise GTFSValidationError(f"stop_times.txt: references unknown trip_id '{t_id}'")
        if s_id not in stop_ids:
            raise GTFSValidationError(f"stop_times.txt: references unknown stop_id '{s_id}'")

        stop_times.append({
            "trip_id": t_id,
            "stop_id": s_id,
            "stop_sequence": int(seq_str),
            "arrival_time": _validate_time(arr_time, "stop_times.txt", "arrival_time"),
            "departure_time": _validate_time(dep_time, "stop_times.txt", "departure_time"),
            "pickup_type": int(r.get("pickup_type") or 0),
            "drop_off_type": int(r.get("drop_off_type") or 0),
        })
    summary.stop_times_count = len(stop_times)

    # 8. Parse Shapes (optional)
    shapes = []
    if "shapes.txt" in file_names:
        shape_rows = _read_csv_from_zip(zf, "shapes.txt")
        for r in shape_rows:
            sh_id = r.get("shape_id")
            if not sh_id:
                continue
            try:
                lat = float(r["shape_pt_lat"])
                lon = float(r["shape_pt_lon"])
                seq = int(r["shape_pt_sequence"])
            except (KeyError, ValueError, TypeError) as exc:
                continue
            shapes.append({
                "shape_id": sh_id,
                "shape_pt_lat": lat,
                "shape_pt_lon": lon,
                "shape_pt_sequence": seq,
            })
    summary.shapes_count = len(shapes)

    payload = {
        "summary": summary,
        "agencies": agencies,
        "routes": routes,
        "stops": stops,
        "trips": trips,
        "stop_times": stop_times,
        "calendar": calendars,
        "calendar_dates": calendar_dates,
        "shapes": shapes,
    }
    return payload, summary


def import_gtfs_feed(
    session: Session | None = None,
    zip_bytes: bytes = b"",
    feed_id: str | None = None,
    agency_id: str | None = None,
    set_as_active: bool = True,
    *,
    db: Session | None = None,
) -> GTFSImportSummary:
    """Transactionally persist an entire validated GTFS feed into the database."""
    active_session = session or db
    if active_session is None:
        raise ValueError("A database session is required to import GTFS feed")

    payload, summary = parse_and_validate_gtfs(
        zip_bytes, feed_id=feed_id, agency_id_override=agency_id
    )

    f_id = summary.feed_id

    # If this feed_id exists, delete previous cascade
    active_session.execute(delete(GTFSFeed).where(GTFSFeed.feed_id == f_id))

    # If setting as active, mark other feeds for this agency inactive
    if set_as_active:
        from sqlalchemy import update
        active_session.execute(
            update(GTFSFeed)
            .where(GTFSFeed.agency_id == summary.agency_id)
            .values(is_active=False)
        )

    import json
    feed_obj = GTFSFeed(
        feed_id=f_id,
        agency_id=summary.agency_id,
        feed_version=None,
        imported_at_utc=datetime.fromisoformat(summary.imported_at_utc),
        sha256=summary.sha256,
        summary_json=json.dumps(summary.to_dict()),
        is_active=set_as_active,
    )
    active_session.add(feed_obj)

    # Insert agencies
    active_session.add_all([GTFSAgency(feed_id=f_id, **a) for a in payload["agencies"]])
    # Insert routes
    active_session.add_all([GTFSRoute(feed_id=f_id, **r) for r in payload["routes"]])
    # Insert stops
    active_session.add_all([GTFSStop(feed_id=f_id, **s) for s in payload["stops"]])
    # Insert calendar
    active_session.add_all([GTFSCalendar(feed_id=f_id, **c) for c in payload["calendar"]])
    # Insert calendar dates
    active_session.add_all([GTFSCalendarDate(feed_id=f_id, **cd) for cd in payload["calendar_dates"]])
    # Insert trips
    active_session.add_all([GTFSTrip(feed_id=f_id, **t) for t in payload["trips"]])
    # Insert stop times in batches
    active_session.add_all([GTFSStopTime(feed_id=f_id, **st) for st in payload["stop_times"]])
    # Insert shapes
    if payload["shapes"]:
        active_session.add_all([GTFSShape(feed_id=f_id, **sh) for sh in payload["shapes"]])

    active_session.flush()
    return summary
