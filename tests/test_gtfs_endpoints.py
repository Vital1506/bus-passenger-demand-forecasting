"""API checks for GTFS Schedule upload and read endpoints."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile

from fastapi.testclient import TestClient

from src import app as app_module
from src import db
from src.db_admin import upgrade_database

KEY = "gtfs-endpoint-test-key"


def build_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "agency.txt",
            "agency_id,agency_name,agency_timezone\nCTA,Test Transit,America/Chicago\n",
        )
        archive.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type\nR1,1,Main Line,3\nR2,2,Crosstown,3\n",
        )
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\nS1,First Street,41.8800,-87.6300\nS2,Second Street,41.9000,-87.6500\n",
        )
        archive.writestr("trips.txt", "route_id,service_id,trip_id\nR1,WK,T1\nR2,WK,T2\n")
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,06:00:00,06:00:00,S1,1\nT1,06:10:00,06:10:00,S2,2\n"
            "T2,25:05:00,25:05:00,S2,1\n",
        )
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WK,1,1,1,1,1,0,0,20260101,20261231\n",
        )
    return buffer.getvalue()


class GtfsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gtfs-endpoints-")
        self._saved = {
            name: os.environ.get(name)
            for name in ("DATABASE_URL", "BUS_FORECAST_REQUIRE_AUTH", "BUS_FORECAST_API_KEY")
        }
        os.environ["DATABASE_URL"] = f"sqlite:///{self.root}/api.db"
        os.environ["BUS_FORECAST_REQUIRE_AUTH"] = "true"
        os.environ["BUS_FORECAST_API_KEY"] = KEY
        db.reset_engine()
        upgrade_database()
        self.client = TestClient(app_module.app)
        self.headers = {"Authorization": f"Bearer {KEY}"}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        db.reset_engine()

    def test_upload_requires_operator_key(self):
        response = self.client.post(
            "/api/gtfs/upload",
            files={"file": ("feed.zip", build_zip(), "application/zip")},
        )
        self.assertEqual(response.status_code, 401)

    def test_upload_and_read_feed(self):
        response = self.client.post(
            "/api/gtfs/upload",
            headers=self.headers,
            files={"file": ("feed.zip", build_zip(), "application/zip")},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["routes"], 2)
        self.assertEqual(body["stops"], 2)
        feed_id = body["feed_id"]

        feeds = self.client.get("/api/gtfs/feeds", headers=self.headers).json()
        self.assertEqual(feeds["count"], 1)
        self.assertEqual(feeds["feeds"][0]["feed_id"], feed_id)

        agencies = self.client.get("/api/gtfs/agencies", headers=self.headers).json()
        self.assertEqual(agencies["count"], 1)
        self.assertEqual(agencies["agencies"][0]["route_count"], 2)

        routes = self.client.get("/api/gtfs/routes", headers=self.headers).json()
        self.assertEqual(routes["total"], 2)

        detail = self.client.get("/api/gtfs/routes/R1", headers=self.headers).json()
        self.assertEqual(detail["route_id"], "R1")
        self.assertEqual(detail["stop_count"], 2)

        stops = self.client.get("/api/gtfs/stops", headers=self.headers).json()
        self.assertEqual(stops["total"], 2)

    def test_invalid_upload_is_rejected(self):
        bad = io.BytesIO(b"not a zip")
        response = self.client.post(
            "/api/gtfs/upload",
            headers=self.headers,
            files={"file": ("bad.zip", bad.getvalue(), "application/zip")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("issues", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()