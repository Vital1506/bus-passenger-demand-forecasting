"""GTFS Schedule (static) ingestion with validation and versioned imports.

Safety rules:
* ZIPs are read in memory with a hard byte limit, an entry-count limit and a
  per-entry uncompressed limit; absolute paths, parent-directory segments and
  symlinks are rejected before anything is parsed.
* Every GTFS file is validated (required columns, identifier formats,
  coordinate ranges, calendar dates, HH:MM:SS times, foreign keys).
* Invalid feeds are rejected with a bounded list of specific issues; nothing is
  written to the database.
* Each import creates a new ``gtfs_feeds`` version. The newest feed per agency
  is the active one used by the API, so re-importing never duplicates the
  visible route/stop catalog while the previous version stays for history.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import delete, func, select

from .schema import (
    GtfsCalendar,
    GtfsCalendarDate,
    GtfsFeed,
    GtfsRoute,
    GtfsShapePoint,
    GtfsStop,
    GtfsStopTime,
    GtfsTrip,
)

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 300
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
MAX_ISSUES = 25
MAX_ROWS = 2_000_000

REQUIRED_FILES = ("agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt")
REQUIRED_COLUMNS = {
    "agency.txt": ("agency_name",),
    "routes.txt": ("route_id", "route_type"),
    "stops.txt": ("stop_id", "stop_name"),
    "trips.txt": ("route_id", "service_id", "trip_id"),
    "stop_times.txt": ("trip_id", "stop_id", "stop_sequence"),
    "calendar.txt": ("service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"),
    "calendar_dates.txt": ("service_id", "date", "exception_type"),
    "shapes.txt": ("shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"),
}
TIME_PATTERN = re.compile(r"^\d{1,2}:[0-5]\d:[0-5]\d$")
DATE_PATTERN = re.compile(r"^\d{8}$")


class GtfsValidationError(ValueError):
    """Raised when a feed cannot be accepted; ``issues`` lists specifics."""

    def __init__(self, issues: list[str]):
        self.issues = issues[:MAX_ISSUES]
        super().__init__("; ".join(self.issues[:5]) or "Invalid GTFS feed")


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-").upper()
    return cleaned[:64] or "AGENCY"


def _open_zip(contents: bytes) -> zipfile.ZipFile:
    if not contents:
        raise GtfsValidationError(["The uploaded file is empty"])
    if len(contents) > MAX_ZIP_BYTES:
        raise GtfsValidationError([f"ZIP exceeds the {MAX_ZIP_BYTES // (1024 * 1024)} MB limit"])
    try:
        archive = zipfile.ZipFile(io.BytesIO(contents))
    except zipfile.BadZipFile as exc:
        raise GtfsValidationError(["The file is not a valid ZIP archive"]) from exc
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise GtfsValidationError([f"ZIP contains more than {MAX_ENTRIES} entries"])
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
            raise GtfsValidationError([f"Unsafe path in archive: {name}"])
        if info.is_dir():
            continue
        total += info.file_size
        if info.file_size > MAX_UNCOMPRESSED_BYTES:
            raise GtfsValidationError([f"Uncompressed entry is too large: {name}"])
        if total > MAX_UNCOMPRESSED_BYTES:
            raise GtfsValidationError(["Uncompressed archive exceeds the size limit"])
    return archive


def _table(archive: zipfile.ZipFile, name: str) -> pd.DataFrame:
    if name not in archive.namelist():
        return pd.DataFrame()
    with archive.open(name) as handle:
        frame = pd.read_csv(handle, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing = [column for column in REQUIRED_COLUMNS[name] if column not in frame.columns]
    if missing:
        raise GtfsValidationError([f"{name}: missing required column(s) {', '.join(missing)}"])
    if len(frame) > MAX_ROWS:
        raise GtfsValidationError([f"{name}: more than {MAX_ROWS} rows"])
    frame.attrs["source"] = name
    return frame


def parse_feed(contents: bytes) -> dict:
    """Validate a GTFS ZIP and return plain records ready for insertion."""
    archive = _open_zip(contents)
    names = set(archive.namelist())
    missing = [name for name in REQUIRED_FILES if name not in names]
    if missing:
        raise GtfsValidationError([f"missing required file(s): {', '.join(missing)}"])
    if "calendar.txt" not in names and "calendar_dates.txt" not in names:
        raise GtfsValidationError(["missing calendar.txt or calendar_dates.txt"])

    issues: list[str] = []
    agency = _table(archive, "agency.txt")
    routes = _table(archive, "routes.txt")
    stops = _table(archive, "stops.txt")
    trips = _table(archive, "trips.txt")
    stop_times = _table(archive, "stop_times.txt")
    calendars = _table(archive, "calendar.txt")
    calendar_dates = _table(archive, "calendar_dates.txt")
    shapes = _table(archive, "shapes.txt") if "shapes.txt" in names else pd.DataFrame()

    if agency.empty:
        raise GtfsValidationError(["agency.txt contains no agencies"])
    agency_name = agency.iloc[0]["agency_name"].strip()
    if not agency_name:
        raise GtfsValidationError(["agency.txt: agency_name is blank"])
    agency_code = _slug(agency.iloc[0].get("agency_id", "") or agency_name)
    timezone = str(agency.iloc[0].get("agency_timezone", "")).strip() or None

    for column in ("route_id", "stop_id", "trip_id"):
        frame = {"route_id": routes, "stop_id": stops, "trip_id": trips}[column]
        if frame[column].str.strip().eq("").any():
            issues.append(f"{frame.attrs['source']}: blank {column}")
    if routes["route_id"].duplicated().any():
        issues.append("routes.txt: duplicate route_id values")
    if stops["stop_id"].duplicated().any():
        issues.append("stops.txt: duplicate stop_id values")
    if trips["trip_id"].duplicated().any():
        issues.append("trips.txt: duplicate trip_id values")

    if "stop_lat" not in stops.columns or "stop_lon" not in stops.columns:
        issues.append("stops.txt: missing stop_lat/stop_lon columns")
        latitude = pd.Series(dtype=float)
        longitude = pd.Series(dtype=float)
    else:
        latitude = pd.to_numeric(stops["stop_lat"], errors="coerce")
        longitude = pd.to_numeric(stops["stop_lon"], errors="coerce")
    if latitude.empty and "stops.txt: missing stop_lat/stop_lon columns" in issues:
        pass
    elif latitude.isna().any() or longitude.isna().any():
        issues.append("stops.txt: missing or non-numeric stop_lat/stop_lon")
    elif ((latitude < -90) | (latitude > 90) | (longitude < -180) | (longitude > 180)).any():
        issues.append("stops.txt: coordinates outside valid ranges")

    sequences = pd.to_numeric(stop_times["stop_sequence"], errors="coerce")
    if sequences.isna().any():
        issues.append("stop_times.txt: non-numeric stop_sequence")
    for column in ("arrival_time", "departure_time"):
        if column in stop_times.columns:
            values = stop_times[column].astype(str)
            invalid = ~values.eq("") & ~values.str.match(TIME_PATTERN)
            if invalid.any():
                issues.append(f"stop_times.txt: invalid {column} format")

    known_routes = set(routes["route_id"])
    if not set(trips["route_id"]).issubset(known_routes):
        issues.append("trips.txt: references unknown route_id")
    known_trips = set(trips["trip_id"])
    if not set(stop_times["trip_id"]).issubset(known_trips):
        issues.append("stop_times.txt: references unknown trip_id")
    known_stops = set(stops["stop_id"])
    if not set(stop_times["stop_id"]).issubset(known_stops):
        issues.append("stop_times.txt: references unknown stop_id")

    service_ids = set()
    for frame, label in ((calendars, "calendar.txt"), (calendar_dates, "calendar_dates.txt")):
        if not frame.empty:
            service_ids |= set(frame["service_id"])
            if "start_date" in frame.columns and "end_date" in frame.columns:
                for column in ("start_date", "end_date"):
                    bad = ~frame[column].astype(str).str.match(DATE_PATTERN)
                    if bad.any():
                        issues.append(f"{label}: invalid {column} format")
            if "date" in frame.columns:
                bad = ~frame["date"].astype(str).str.match(DATE_PATTERN)
                if bad.any():
                    issues.append(f"{label}: invalid date format")
    if not set(trips["service_id"]).issubset(service_ids):
        issues.append("trips.txt: references service_id missing from calendar files")

    if issues:
        raise GtfsValidationError(issues)

    def _int(value, default=None):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def _date(value: str) -> date | None:
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except (TypeError, ValueError):
            return None

    route_rows = [
        {
            "agency_code": agency_code,
            "route_id": str(row.route_id).strip(),
            "short_name": str(getattr(row, "route_short_name", "") or "").strip() or None,
            "long_name": str(getattr(row, "route_long_name", "") or "").strip() or None,
            "route_type": _int(getattr(row, "route_type", None)),
            "color": str(getattr(row, "route_color", "") or "").strip() or None,
        }
        for row in routes.itertuples()
    ]
    stop_rows = [
        {
            "agency_code": agency_code,
            "stop_id": str(row.stop_id).strip(),
            "name": str(row.stop_name).strip(),
            "latitude": float(row.stop_lat),
            "longitude": float(row.stop_lon),
            "location_type": _int(getattr(row, "location_type", None)),
        }
        for row in stops.itertuples()
    ]
    trip_rows = [
        {
            "agency_code": agency_code,
            "trip_id": str(row.trip_id).strip(),
            "route_id": str(row.route_id).strip(),
            "service_id": str(row.service_id).strip(),
            "headsign": str(getattr(row, "trip_headsign", "") or "").strip() or None,
            "direction_id": _int(getattr(row, "direction_id", None)),
            "shape_id": str(getattr(row, "shape_id", "") or "").strip() or None,
        }
        for row in trips.itertuples()
    ]
    stop_time_rows = [
        {
            "trip_id": str(row.trip_id).strip(),
            "stop_id": str(row.stop_id).strip(),
            "stop_sequence": int(row.stop_sequence),
            "arrival_time": str(getattr(row, "arrival_time", "") or "").strip() or None,
            "departure_time": str(getattr(row, "departure_time", "") or "").strip() or None,
        }
        for row in stop_times.itertuples()
    ]
    calendar_rows = []
    for row in calendars.itertuples():
        calendar_rows.append(
            {
                "service_id": str(row.service_id).strip(),
                "monday": str(getattr(row, "monday", "0")) == "1",
                "tuesday": str(getattr(row, "tuesday", "0")) == "1",
                "wednesday": str(getattr(row, "wednesday", "0")) == "1",
                "thursday": str(getattr(row, "thursday", "0")) == "1",
                "friday": str(getattr(row, "friday", "0")) == "1",
                "saturday": str(getattr(row, "saturday", "0")) == "1",
                "sunday": str(getattr(row, "sunday", "0")) == "1",
                "start_date": _date(row.start_date),
                "end_date": _date(row.end_date),
            }
        )
    calendar_date_rows = [
        {
            "service_id": str(row.service_id).strip(),
            "date": _date(row.date),
            "exception_type": _int(getattr(row, "exception_type", None), 0),
        }
        for row in calendar_dates.itertuples()
    ] if not calendar_dates.empty else []
    shape_rows = [
        {
            "shape_id": str(row.shape_id).strip(),
            "sequence": int(row.shape_pt_sequence),
            "latitude": float(row.shape_pt_lat),
            "longitude": float(row.shape_pt_lon),
        }
        for row in shapes.itertuples()
    ] if not shapes.empty else []

    return {
        "agency_code": agency_code,
        "agency_name": agency_name,
        "timezone": timezone,
        "routes": route_rows,
        "stops": stop_rows,
        "trips": trip_rows,
        "stop_times": stop_time_rows,
        "calendars": calendar_rows,
        "calendar_dates": calendar_date_rows,
        "shapes": shape_rows,
        "warnings": [],
    }


def import_feed(session, contents: bytes, file_name: str, source_checksum: str) -> dict:
    """Validate and store one feed version inside the caller's transaction."""
    parsed = parse_feed(contents)
    calendar_starts = [row["start_date"] for row in parsed["calendars"] if row.get("start_date")]
    calendar_ends = [row["end_date"] for row in parsed["calendars"] if row.get("end_date")]
    feed = GtfsFeed(
        agency_code=parsed["agency_code"],
        checksum=source_checksum,
        file_name=Path(file_name).name[:300],
        feed_start=min(calendar_starts) if calendar_starts else None,
        feed_end=max(calendar_ends) if calendar_ends else None,
        route_count=len(parsed["routes"]),
        stop_count=len(parsed["stops"]),
        trip_count=len(parsed["trips"]),
        stop_time_count=len(parsed["stop_times"]),
        status="imported",
        warnings={"items": parsed["warnings"]},
    )
    session.add(feed)
    session.flush()

    session.add_all([GtfsRoute(feed_id=feed.id, **row) for row in parsed["routes"]])
    session.add_all([GtfsStop(feed_id=feed.id, **row) for row in parsed["stops"]])
    session.add_all([GtfsTrip(feed_id=feed.id, **row) for row in parsed["trips"]])
    session.add_all([GtfsStopTime(feed_id=feed.id, **row) for row in parsed["stop_times"]])
    session.add_all([GtfsCalendar(feed_id=feed.id, **row) for row in parsed["calendars"]])
    session.add_all([GtfsCalendarDate(feed_id=feed.id, **row) for row in parsed["calendar_dates"]])
    session.add_all([GtfsShapePoint(feed_id=feed.id, **row) for row in parsed["shapes"]])

    return {
        "feed_id": feed.id,
        "agency_code": parsed["agency_code"],
        "agency_name": parsed["agency_name"],
        "timezone": parsed["timezone"],
        "feed_start": str(feed.feed_start) if feed.feed_start else None,
        "feed_end": str(feed.feed_end) if feed.feed_end else None,
        "routes": len(parsed["routes"]),
        "stops": len(parsed["stops"]),
        "trips": len(parsed["trips"]),
        "stop_times": len(parsed["stop_times"]),
        "calendars": len(parsed["calendars"]),
        "calendar_dates": len(parsed["calendar_dates"]),
        "shape_points": len(parsed["shapes"]),
        "warnings": parsed["warnings"],
    }


def active_feed_id(session, agency_code: str) -> int | None:
    """Newest imported feed for an agency is the active catalog."""
    return session.scalar(
        select(func.max(GtfsFeed.id)).where(GtfsFeed.agency_code == agency_code)
    )


def delete_feed(session, feed_id: int) -> int:
    """Remove one feed version and its child rows (explicit admin action)."""
    children = (GtfsShapePoint, GtfsCalendarDate, GtfsCalendar, GtfsStopTime, GtfsTrip, GtfsStop, GtfsRoute)
    for model in children:
        session.execute(delete(model).where(model.feed_id == feed_id))
    session.execute(delete(GtfsFeed).where(GtfsFeed.id == feed_id))
    return feed_id
