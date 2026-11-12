"""GTFS Schedule validation, import versioning and refresh behaviour."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from sqlalchemy import func, select

from src import db
from src.db_admin import upgrade_database
from src.gtfs_static import (
    GtfsValidationError,
    active_feed_id,
    delete_feed,
    import_feed,
    parse_feed,
)
from src.schema import GtfsFeed, GtfsRoute, GtfsStop, GtfsStopTime, GtfsTrip

BASE_FILES = {
    "agency.txt": "agency_id,agency_name,agency_timezone\nCTA,Test Transit,America/Chicago\n",
    "routes.txt": (
        "route_id,route_short_name,route_long_name,route_type\n"
        "R1,1,Main Line,3\n"
        "R2,2,Crosstown,3\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "S1,First Street,41.8800,-87.6300\n"
        "S2,Second Street,41.9000,-87.6500\n"
    ),
    "trips.txt": "route_id,service_id,trip_id\nR1,WK,T1\nR2,WK,T2\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,06:00:00,06:00:00,S1,1\n"
        "T1,06:10:00,06:10:00,S2,2\n"
        "T2,25:05:00,25:05:00,S2,1\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WK,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
}


def build_zip(files: dict[str, str] | None = None, *, drop: tuple[str, ...] = (), extra: dict[str, str] | None = None) -> bytes:
    contents = dict(BASE_FILES if files is None else files)
    for name in drop:
        contents.pop(name, None)
    if extra:
        contents.update(extra)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in contents.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class GtfsParseTests(unittest.TestCase):
    def test_valid_feed_parses_with_reference_relationships(self):
        parsed = parse_feed(build_zip())
        self.assertEqual(parsed["agency_code"], "CTA")
        self.assertEqual(len(parsed["routes"]), 2)
        self.assertEqual(len(parsed["stops"]), 2)
        self.assertEqual(len(parsed["trips"]), 2)
        self.assertEqual(len(parsed["stop_times"]), 3)
        self.assertEqual(parsed["calendars"][0]["start_date"].isoformat(), "2026-01-01")

    def test_missing_required_file_is_rejected(self):
        with self.assertRaises(GtfsValidationError) as caught:
            parse_feed(build_zip(drop=("trips.txt",)))
        self.assertIn("trips.txt", str(caught.exception))

    def test_missing_calendar_files_are_rejected(self):
        with self.assertRaises(GtfsValidationError) as caught:
            parse_feed(build_zip(drop=("calendar.txt",)))
        self.assertIn("calendar.txt or calendar_dates.txt", str(caught.exception))

    def test_invalid_coordinates_are_rejected(self):
        broken = dict(BASE_FILES)
        broken["stops.txt"] = "stop_id,stop_name,stop_lat,stop_lon\nS1,First,91.5,-87.6\n"
        with self.assertRaises(GtfsValidationError) as caught:
            parse_feed(build_zip(broken))
        self.assertIn("coordinates", str(caught.exception))

    def test_unknown_stop_reference_is_rejected(self):
        broken = dict(BASE_FILES)
        broken["stop_times.txt"] = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,06:00:00,06:00:00,MISSING,1\n"
        with self.assertRaises(GtfsValidationError) as caught:
            parse_feed(build_zip(broken))
        self.assertIn("unknown stop_id", str(caught.exception))

    def test_invalid_time_format_is_rejected(self):
        broken = dict(BASE_FILES)
        broken["stop_times.txt"] = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,6am,6am,S1,1\n"
        with self.assertRaises(GtfsValidationError) as caught:
            parse_feed(build_zip(broken))
        self.assertIn("invalid arrival_time", str(caught.exception))

    def test_unsafe_zip_path_is_rejected(self):
        payload = build_zip(extra={"../escape.txt": "nope"})
        with self.assertRaises(GtfsValidationError) as caught:
            parse_feed(payload)
        self.assertIn("Unsafe path", str(caught.exception))

    def test_non_zip_payload_is_rejected(self):
        with self.assertRaises(GtfsValidationError):
            parse_feed(b"this is not a zip")


class GtfsImportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gtfs-import-"))
        self._saved = {name: os.environ.get(name) for name in ("DATABASE_URL", "BUS_FORECAST_DATA_DIR")}
        os.environ.pop("DATABASE_URL", None)
        os.environ["BUS_FORECAST_DATA_DIR"] = str(self.root)
        db.reset_engine()
        upgrade_database()

    def tearDown(self):
        db.reset_engine()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        db.reset_engine()

    def test_import_creates_versioned_rows(self):
        with db.session_scope() as session:
            summary = import_feed(session, build_zip(), "feed.zip", "checksum-1")
        self.assertEqual(summary["routes"], 2)
        with db.session_scope() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(GtfsRoute)), 2)
            self.assertEqual(session.scalar(select(func.count()).select_from(GtfsStop)), 2)
            self.assertEqual(session.scalar(select(func.count()).select_from(GtfsTrip)), 2)
            self.assertEqual(session.scalar(select(func.count()).select_from(GtfsStopTime)), 3)

    def test_refresh_uses_newest_feed_without_duplicating_catalog(self):
        with db.session_scope() as session:
            first = import_feed(session, build_zip(), "feed.zip", "checksum-1")
        with db.session_scope() as session:
            second = import_feed(session, build_zip(), "feed-v2.zip", "checksum-2")
        self.assertNotEqual(first["feed_id"], second["feed_id"])
        with db.session_scope() as session:
            active = active_feed_id(session, "CTA")
            self.assertEqual(active, second["feed_id"])
            self.assertEqual(session.scalar(select(func.count()).select_from(GtfsFeed)), 2)
            active_routes = session.scalar(
                select(func.count()).select_from(GtfsRoute).where(GtfsRoute.feed_id == active)
            )
            self.assertEqual(active_routes, 2)

    def test_delete_feed_removes_children_only(self):
        with db.session_scope() as session:
            first = import_feed(session, build_zip(), "feed.zip", "checksum-1")
        with db.session_scope() as session:
            import_feed(session, build_zip(), "feed-v2.zip", "checksum-2")
        with db.session_scope() as session:
            delete_feed(session, first["feed_id"])
        with db.session_scope() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(GtfsFeed)), 1)
            remaining = session.scalar(select(GtfsRoute.feed_id))
            self.assertNotEqual(remaining, first["feed_id"])


if __name__ == "__main__":
    unittest.main()
