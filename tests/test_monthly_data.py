"""Service-level checks for the independent monthly passenger store."""

import json
import tempfile
import unittest
from pathlib import Path

from src.monthly_data import (
    MonthlyDataError,
    build_monthly_status,
    get_monthly_route_history,
    ingest_monthly_csv,
    list_monthly_routes,
    load_monthly_store,
    monthly_audit_path,
    monthly_store_path,
    read_monthly_audit,
    validate_monthly_csv,
)

HEADER = "month_start,route_id,route_name,boardings"


def csv_bytes(rows, header=HEADER):
    lines = [header] + [",".join(str(value) for value in row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


class MonthlyStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="monthly-store-test-"))
        self.store_path = monthly_store_path(self.root)
        self.audit_path = monthly_audit_path(self.root)

    def upload(self, rows, header=HEADER, name="monthly.csv"):
        return ingest_monthly_csv(self.store_path, self.audit_path, csv_bytes(rows, header), file_name=name)

    def test_valid_records_are_stored_with_string_route_ids(self):
        result = self.upload([
            ("2024-01-01", "8A", "Halsted", 1200),
            ("2024-01-01", "10", "Museum", 300),
            ("2024-02-01", "8A", "Halsted", 1500),
        ])
        self.assertEqual(result["import"]["inserted_records"], 3)
        self.assertEqual(result["import"]["row_count_after"], 3)
        self.assertEqual(result["import"]["route_count_after"], 2)
        self.assertFalse(result["import"]["idempotent"])

        store = load_monthly_store(self.store_path)
        self.assertIn("8A", store["routes"])
        self.assertIn("10", store["routes"])
        self.assertIsInstance(store["routes"]["8A"]["records"]["2024-01-01"]["boardings"], int)
        self.assertEqual(store["routes"]["8A"]["route_name"], "Halsted")
        self.assertEqual(store["totals"]["first_month"], "2024-01-01")
        self.assertEqual(store["totals"]["last_month"], "2024-02-01")

        # The file hash is recorded and is the hash of the uploaded bytes.
        raw = csv_bytes([("2024-01-01", "8A", "Halsted", 1200)])
        _, digest = validate_monthly_csv(raw)
        self.assertEqual(len(digest), 64)
        self.assertEqual(store["source"]["file_sha256"], result["import"]["file_sha256"])

    def test_invalid_headers_are_rejected(self):
        with self.assertRaisesRegex(MonthlyDataError, "Missing required CSV columns: boardings"):
            self.upload([("2024-01-01", "8A", "Halsted")], header="month_start,route_id,route_name")
        with self.assertRaisesRegex(MonthlyDataError, "Missing required CSV columns"):
            self.upload([("2024-01-01", "8A", "Halsted", 10)], header="date,route_id,route_name,boardings")
        with self.assertRaisesRegex(MonthlyDataError, "duplicate column names"):
            self.upload([("2024-01-01", "8A", "Halsted", 10)], header=f"{HEADER},boardings")

    def test_invalid_dates_are_rejected(self):
        for bad in ("2024-13-01", "2024-02-30", "2024-1-1", "01/01/2024", "2024-01-15"):
            with self.subTest(month_start=bad):
                with self.assertRaises(MonthlyDataError):
                    self.upload([(bad, "8A", "Halsted", 10)])

    def test_invalid_boarding_values_are_rejected(self):
        for bad in ("-5", "1.5", "nan", "inf", "abc", ""):
            with self.subTest(boardings=bad):
                with self.assertRaises(MonthlyDataError):
                    self.upload([("2024-01-01", "8A", "Halsted", bad)])

    def test_blank_route_values_are_rejected(self):
        with self.assertRaisesRegex(MonthlyDataError, "route_id cannot be blank"):
            self.upload([("2024-01-01", "  ", "Halsted", 10)])
        with self.assertRaisesRegex(MonthlyDataError, "route_name cannot be blank"):
            self.upload([("2024-01-01", "8A", "", 10)])

    def test_duplicate_route_month_keys_are_rejected(self):
        with self.assertRaisesRegex(MonthlyDataError, "Duplicate route-month record"):
            self.upload([
                ("2024-01-01", "8A", "Halsted", 10),
                ("2024-01-01", "8A", "Halsted", 20),
            ])
        self.assertFalse(self.store_path.exists())

    def test_missing_months_are_preserved_as_absent(self):
        self.upload([
            ("2024-01-01", "8A", "Halsted", 1100),
            ("2024-03-01", "8A", "Halsted", 1300),
        ])
        history = get_monthly_route_history(load_monthly_store(self.store_path), "8A")
        months = [record["month_start"] for record in history["records"]]
        self.assertEqual(months, ["2024-01-01", "2024-03-01"])
        self.assertNotIn("2024-02-01", self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(len(history["records"]), 2)

    def test_route_name_changes_are_kept_per_month(self):
        self.upload([
            ("2012-10-01", "1", "Indiana/Hyde Park", 100),
            ("2012-11-01", "1", "Bronzeville/Union Station", 200),
            ("2012-12-01", "1", "Bronzeville/Union Station", 250),
        ])
        history = get_monthly_route_history(load_monthly_store(self.store_path), "1")
        self.assertEqual(history["route_name"], "Bronzeville/Union Station")
        self.assertEqual(len(history["name_history"]), 2)
        self.assertEqual(history["name_history"][0]["route_name"], "Indiana/Hyde Park")
        self.assertEqual(history["name_history"][0]["first_month"], "2012-10-01")
        self.assertEqual(history["name_history"][0]["last_month"], "2012-10-01")
        self.assertEqual(history["name_history"][1]["month_count"], 2)

    def test_repeated_upload_is_idempotent(self):
        rows = [("2024-01-01", "8A", "Halsted", 1200), ("2024-02-01", "8A", "Halsted", 1300)]
        first = self.upload(rows)
        second = self.upload(rows)
        self.assertEqual(first["import"]["inserted_records"], 2)
        self.assertEqual(second["import"]["inserted_records"], 0)
        self.assertEqual(second["import"]["updated_records"], 0)
        self.assertEqual(second["import"]["unchanged_records"], 2)
        self.assertTrue(second["import"]["idempotent"])
        self.assertEqual(second["import"]["row_count_after"], 2)

    def test_historical_records_survive_later_uploads(self):
        self.upload([("2024-01-01", "8A", "Halsted", 100), ("2024-02-01", "8A", "Halsted", 200)])
        result = self.upload([("2024-03-01", "8A", "Halsted", 300), ("2024-03-01", "9", "Ashland", 400)])
        self.assertEqual(result["import"]["inserted_records"], 2)
        self.assertEqual(result["import"]["unchanged_records"], 0)
        history = get_monthly_route_history(load_monthly_store(self.store_path), "8A")
        self.assertEqual([record["boardings"] for record in history["records"]], [100, 200, 300])

    def test_corrected_records_replace_values_and_are_tracked(self):
        self.upload([("2024-01-01", "8A", "Halsted", 100)])
        result = self.upload([("2024-01-01", "8A", "Halsted", 175)])
        self.assertEqual(result["import"]["updated_records"], 1)
        self.assertEqual(result["import"]["inserted_records"], 0)
        self.assertEqual(result["import"]["corrections_recorded"], 1)
        self.assertFalse(result["import"]["idempotent"])

        store = load_monthly_store(self.store_path)
        self.assertEqual(store["routes"]["8A"]["records"]["2024-01-01"]["boardings"], 175)
        correction = store["corrections"][0]
        self.assertEqual(correction["field"], "boardings")
        self.assertEqual(correction["previous"], 100)
        self.assertEqual(correction["current"], 175)

        events = read_monthly_audit(self.audit_path)
        self.assertEqual(events[0]["updated_records"], 1)
        self.assertEqual(events[0]["action"], "monthly_upload")
        self.assertNotIn("175", json.dumps(events))  # aggregate-only audit, never passenger rows

    def test_empty_upload_is_rejected(self):
        with self.assertRaisesRegex(MonthlyDataError, "header but no data records"):
            self.upload([])

    def test_empty_store_status_and_route_index(self):
        store = load_monthly_store(self.store_path)
        status = build_monthly_status(store)
        self.assertFalse(status["data_loaded"])
        self.assertEqual(status["row_count"], 0)
        self.assertEqual(status["route_count"], 0)
        self.assertEqual(list_monthly_routes(store), [])
        self.assertIsNone(get_monthly_route_history(store, "8A"))

    def test_store_is_written_atomically_without_leftovers(self):
        self.upload([("2024-01-01", "8A", "Halsted", 10)])
        leftovers = [path.name for path in self.store_path.parent.iterdir() if path.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        payload = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["dataset"], "cta_monthly_ridership")


if __name__ == "__main__":
    unittest.main()
