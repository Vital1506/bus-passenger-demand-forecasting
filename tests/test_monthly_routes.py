"""API checks for the monthly passenger endpoints and their access rules."""

import os
import tempfile
import unittest
from pathlib import Path

# The application resolves its data directory at import time, so isolate it
# before importing the module under test.
os.environ["BUS_FORECAST_DATA_DIR"] = tempfile.mkdtemp(prefix="bus-forecast-monthly-api-")
os.environ.pop("BUS_FORECAST_PUBLIC_MONTHLY_READ", None)

from fastapi.testclient import TestClient  # noqa: E402

from src import app as app_module  # noqa: E402

HEADER = "month_start,route_id,route_name,boardings"
ADMIN_KEY = "test-admin-key"
ROWS = [
    ("2024-01-01", "8A", "Halsted", 1200),
    ("2024-01-01", "10", "Museum Campus", 300),
    ("2024-02-01", "8A", "Halsted", 1500),
]


def csv_bytes(rows, header=HEADER):
    lines = [header] + [",".join(str(value) for value in row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


class MonthlyEndpointTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="monthly-api-case-"))
        self.store_path = root / "monthly" / "cta_monthly_ridership.json"
        self.audit_path = root / "monthly" / "ingest_audit.jsonl"
        self._saved_env = {
            name: os.environ.get(name)
            for name in (
                "BUS_FORECAST_REQUIRE_AUTH",
                "BUS_FORECAST_API_KEY",
                "BUS_FORECAST_PUBLIC_MONTHLY_READ",
            )
        }
        self._saved_paths = (app_module.MONTHLY_STORE_PATH, app_module.MONTHLY_AUDIT_PATH)
        app_module.MONTHLY_STORE_PATH = self.store_path
        app_module.MONTHLY_AUDIT_PATH = self.audit_path
        os.environ["BUS_FORECAST_REQUIRE_AUTH"] = "true"
        os.environ["BUS_FORECAST_API_KEY"] = ADMIN_KEY
        os.environ.pop("BUS_FORECAST_PUBLIC_MONTHLY_READ", None)
        self.client = TestClient(app_module.app)
        self.admin = {"Authorization": f"Bearer {ADMIN_KEY}"}

    def tearDown(self):
        app_module.MONTHLY_STORE_PATH, app_module.MONTHLY_AUDIT_PATH = self._saved_paths
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def upload(self, rows=ROWS, header=HEADER, name="monthly.csv", headers=None):
        return self.client.post(
            "/api/monthly-data/upload",
            files={"file": (name, csv_bytes(rows, header), "text/csv")},
            headers=self.admin if headers is None else headers,
        )

    # ---------------------------------------------------------------- upload

    def test_upload_requires_the_administrator_key(self):
        self.assertEqual(self.upload(headers={}).status_code, 401)
        self.assertEqual(self.upload(headers={"Authorization": "Bearer wrong-key"}).status_code, 401)
        self.assertEqual(self.upload().status_code, 200)

    def test_upload_writes_only_the_monthly_store(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["import"]["inserted_records"], 3)
        self.assertEqual(body["status"]["row_count"], 3)
        self.assertTrue(self.store_path.exists())
        self.assertTrue(self.audit_path.exists())
        # The daily dataset and its metadata are never touched by monthly imports.
        self.assertFalse(app_module.DATA_PATH.exists())
        self.assertFalse(app_module.META_PATH.exists())

    def test_upload_rejects_invalid_headers_dates_negatives_and_duplicates(self):
        cases = {
            "headers": csv_bytes(ROWS, header="month_start,route_id,route_name"),
            "date": csv_bytes([("2024-02-30", "8A", "Halsted", 10)]),
            "negative": csv_bytes([("2024-01-01", "8A", "Halsted", -1)]),
            "duplicate": csv_bytes([
                ("2024-01-01", "8A", "Halsted", 10),
                ("2024-01-01", "8A", "Halsted", 20),
            ]),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                response = self.client.post(
                    "/api/monthly-data/upload",
                    files={"file": ("monthly.csv", payload, "text/csv")},
                    headers=self.admin,
                )
                self.assertEqual(response.status_code, 422)
                self.assertIn("detail", response.json())
        self.assertFalse(self.store_path.exists())

    def test_upload_rejects_non_csv_files(self):
        response = self.client.post(
            "/api/monthly-data/upload",
            files={"file": ("monthly.json", b"{}", "application/json")},
            headers=self.admin,
        )
        self.assertEqual(response.status_code, 415)

    def test_repeated_upload_is_idempotent_over_the_api(self):
        self.upload()
        body = self.upload().json()
        self.assertEqual(body["import"]["inserted_records"], 0)
        self.assertEqual(body["import"]["updated_records"], 0)
        self.assertTrue(body["import"]["idempotent"])
        self.assertEqual(body["status"]["row_count"], 3)

    def test_corrected_records_are_reported(self):
        self.upload()
        body = self.upload([("2024-01-01", "8A", "Halsted", 1250), ("2024-02-01", "8A", "Halsted", 1500)]).json()
        self.assertEqual(body["import"]["updated_records"], 1)
        self.assertEqual(body["import"]["corrections_recorded"], 1)
        status = self.client.get("/api/monthly-data/status", headers=self.admin).json()
        self.assertEqual(status["corrections_recorded"], 1)
        self.assertEqual(len(status["recent_activity"]), 2)

    # ------------------------------------------------------------------ admin

    def test_admin_reads_require_the_administrator_key(self):
        self.upload()
        for path in (
            "/api/monthly-data/status",
            "/api/monthly-data/routes",
            "/api/monthly-data/routes/8A/history",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                self.assertEqual(self.client.get(path, headers=self.admin).status_code, 200)

    def test_routes_index_and_history_payloads(self):
        self.upload()
        routes = self.client.get("/api/monthly-data/routes", headers=self.admin).json()
        self.assertEqual(routes["dataset"], "cta_monthly_ridership")
        self.assertEqual(routes["route_count"], 2)
        self.assertEqual([route["route_id"] for route in routes["routes"]], ["10", "8A"])
        self.assertEqual(routes["routes"][1]["month_count"], 2)
        self.assertEqual(routes["routes"][1]["total_boardings"], 2700)

        history = self.client.get("/api/monthly-data/routes/8A/history", headers=self.admin).json()
        self.assertEqual(history["route_id"], "8A")
        self.assertEqual([record["month_start"] for record in history["records"]], ["2024-01-01", "2024-02-01"])
        self.assertIn("not zero-filled", history["missing_month_policy"])

    def test_route_name_changes_are_visible(self):
        self.upload([
            ("2024-01-01", "1", "Indiana/Hyde Park", 100),
            ("2024-02-01", "1", "Bronzeville/Union Station", 120),
        ])
        history = self.client.get("/api/monthly-data/routes/1/history", headers=self.admin).json()
        self.assertEqual(history["route_name"], "Bronzeville/Union Station")
        self.assertEqual(len(history["name_history"]), 2)

    def test_unknown_route_history_is_404(self):
        self.upload()
        response = self.client.get("/api/monthly-data/routes/ZZZ/history", headers=self.admin)
        self.assertEqual(response.status_code, 404)

    def test_empty_dataset_reads_cleanly(self):
        status = self.client.get("/api/monthly-data/status", headers=self.admin).json()
        self.assertFalse(status["data_loaded"])
        self.assertEqual(status["row_count"], 0)
        routes = self.client.get("/api/monthly-data/routes", headers=self.admin).json()
        self.assertEqual(routes["routes"], [])
        self.assertEqual(self.client.get("/api/monthly-data/routes/8A/history", headers=self.admin).status_code, 404)

    # ----------------------------------------------------------------- public

    def test_public_endpoints_are_disabled_by_default(self):
        self.upload()
        for path in (
            "/api/public/monthly-data/routes",
            "/api/public/monthly-data/routes/8A/history",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_public_endpoints_serve_read_only_when_enabled(self):
        self.upload()
        os.environ["BUS_FORECAST_PUBLIC_MONTHLY_READ"] = "true"
        routes = self.client.get("/api/public/monthly-data/routes")
        self.assertEqual(routes.status_code, 200)
        self.assertEqual(routes.json()["route_count"], 2)
        history = self.client.get("/api/public/monthly-data/routes/8A/history")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["month_count"], 2)
        # Public reads work without a key while the admin API still requires one.
        self.assertEqual(self.client.get("/api/monthly-data/status").status_code, 401)

    def test_only_the_two_public_routes_are_exposed(self):
        self.upload()
        os.environ["BUS_FORECAST_PUBLIC_MONTHLY_READ"] = "true"
        self.assertEqual(self.client.get("/api/public/monthly-data/status").status_code, 404)
        self.assertEqual(self.client.get("/api/public/monthly-data/upload").status_code, 404)
        # Uploading still requires the admin key even with public reads enabled.
        payload = csv_bytes([("2024-03-01", "8A", "Halsted", 100)])
        response = self.client.post(
            "/api/public/monthly-data/upload",
            files={"file": ("monthly.csv", payload, "text/csv")},
        )
        self.assertEqual(response.status_code, 404)

    def test_public_history_unknown_route_is_404(self):
        self.upload()
        os.environ["BUS_FORECAST_PUBLIC_MONTHLY_READ"] = "true"
        self.assertEqual(self.client.get("/api/public/monthly-data/routes/NOPE/history").status_code, 404)

    def test_public_flag_accepts_false_as_disabled(self):
        self.upload()
        os.environ["BUS_FORECAST_PUBLIC_MONTHLY_READ"] = "false"
        self.assertEqual(self.client.get("/api/public/monthly-data/routes").status_code, 404)

    def test_admin_responses_never_expose_the_api_key(self):
        self.upload()
        for path in ("/api/monthly-data/status", "/api/monthly-data/routes"):
            with self.subTest(path=path):
                self.assertNotIn(ADMIN_KEY, self.client.get(path, headers=self.admin).text)


if __name__ == "__main__":
    unittest.main()
