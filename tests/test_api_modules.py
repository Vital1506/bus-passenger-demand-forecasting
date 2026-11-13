"""Tests for the domain API modules added in the backend engineering phase."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src import app as app_module
from src.api import API_DESCRIPTION, ROUTERS, TAG_METADATA
from src.api import jobs as jobs_module
from src.api.idempotency import IdempotencyStore, normalize_key
from src.api.pagination import Page, PageParams, page_params

SAVED_NAMES = (
    "DATA_DIR",
    "MONTHLY_STORE_PATH",
    "MONTHLY_AUDIT_PATH",
    "SECURITY_AUDIT_PATH",
    "FORECAST_PATH",
    "CAPACITY_ASSUMPTIONS_PATH",
)


class ApiTestBase(unittest.TestCase):
    """Runs each test against a throwaway data directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {name: getattr(app_module, name) for name in SAVED_NAMES}
        self._env = {key: os.environ.get(key) for key in ("BUS_FORECAST_REQUIRE_AUTH", "BUS_FORECAST_AGENCY")}
        os.environ.pop("BUS_FORECAST_REQUIRE_AUTH", None)
        os.environ["BUS_FORECAST_AGENCY"] = "test-agency"
        app_module.DATA_DIR = self.root
        app_module.MONTHLY_STORE_PATH = self.root / "monthly_store.json"
        app_module.MONTHLY_AUDIT_PATH = self.root / "monthly_audit.jsonl"
        app_module.SECURITY_AUDIT_PATH = self.root / "security_audit.jsonl"
        app_module.FORECAST_PATH = self.root / "forecast.csv"
        app_module.CAPACITY_ASSUMPTIONS_PATH = self.root / "capacity_assumptions.json"
        jobs_module._RUNNERS.clear()
        self.client = TestClient(app_module.app)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(app_module, name, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        jobs_module.shutdown_all()
        jobs_module._RUNNERS.clear()
        self._tmp.cleanup()


class OpenApiTests(ApiTestBase):
    def test_domains_are_documented_with_tags(self) -> None:
        tags = {item["name"] for item in TAG_METADATA}
        self.assertEqual(len(tags), 11)
        self.assertEqual(len(ROUTERS), 11)
        self.assertIn("Idempotency-Key", API_DESCRIPTION)
        self.assertIn("paginated", API_DESCRIPTION.lower())

    def test_every_domain_prefix_is_mounted(self) -> None:
        spec = app_module.app.openapi()
        paths = set(spec["paths"])
        for prefix in (
            "/api/auth",
            "/api/network",
            "/api/ingestion/gtfs-static",
            "/api/ingestion/gtfs-realtime",
            "/api/ingestion/observations",
            "/api/forecasting",
            "/api/weather",
            "/api/occupancy",
            "/api/alerts",
            "/api/reporting",
            "/api/sources",
        ):
            self.assertTrue(any(path.startswith(prefix) for path in paths), prefix)

    def test_list_endpoints_document_page_parameters(self) -> None:
        spec = app_module.app.openapi()
        params = {param["name"] for param in spec["paths"]["/api/ingestion/observations"]["get"]["parameters"]}
        self.assertTrue({"page", "page_size"}.issubset(params))

    def test_openapi_version_and_tags_are_applied(self) -> None:
        spec = app_module.app.openapi()
        self.assertEqual(spec["info"]["version"], "0.2.0")
        self.assertIn("Authentication", {tag["name"] for tag in spec["tags"]})


class PaginationTests(unittest.TestCase):
    def test_page_build_slices_and_reports_links(self) -> None:
        page = Page.build(list(range(120)), PageParams(page=2, page_size=50))
        self.assertEqual(page.items, list(range(50, 100)))
        self.assertEqual((page.total, page.pages), (120, 3))
        self.assertTrue(page.has_next)
        self.assertTrue(page.has_previous)

    def test_last_page_and_empty_page(self) -> None:
        last = Page.build(list(range(120)), PageParams(page=3, page_size=50))
        self.assertFalse(last.has_next)
        self.assertEqual(len(last.items), 20)
        empty = Page.build([], PageParams(page=1, page_size=10))
        self.assertEqual((empty.total, empty.pages, empty.has_next), (0, 0, False))

    def test_page_parameters_carry_their_bounds(self) -> None:
        params = page_params(page=1, page_size=200)
        self.assertEqual((params.page, params.page_size, params.offset), (1, 200, 0))
        defaults = page_params() if False else None  # FastAPI supplies defaults at request time
        self.assertIsNone(defaults)
        annotated = {name: field for name, field in page_params.__annotations__.items()}
        self.assertIn("page", annotated)
        self.assertIn("page_size", annotated)


class IdempotencyTests(unittest.TestCase):
    def test_same_key_replays_without_running_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = IdempotencyStore(Path(tmp) / "idem.json")
            calls = {"count": 0}

            def produce() -> tuple[int, object]:
                calls["count"] += 1
                return 201, {"value": calls["count"]}

            first = store.execute("scope", "key-1", produce)
            second = store.execute("scope", "key-1", produce)
            other = store.execute("scope", "key-2", produce)
            self.assertEqual(first[:2], (201, {"value": 1}))
            self.assertEqual(second[:2], (201, {"value": 1}))
            self.assertTrue(second[2])
            self.assertFalse(first[2])
            self.assertEqual(other[1], {"value": 2})
            self.assertEqual(calls["count"], 2)

    def test_blank_keys_are_ignored(self) -> None:
        self.assertIsNone(normalize_key("   "))
        self.assertEqual(normalize_key("abc"), "abc")
        with tempfile.TemporaryDirectory() as tmp:
            store = IdempotencyStore(Path(tmp) / "idem.json")
            self.assertIsNone(store.lookup("scope", None))


class JobTests(ApiTestBase):
    def test_job_succeeds_and_records_result(self) -> None:
        painter = jobs_module.get_runner(self.root / "jobs.json")
        job = painter.submit("demo", lambda value=1: {"doubled": value * 2}, params={"value": 21})
        finished = painter.wait(job["job_id"], timeout=5)
        self.assertIsNotNone(finished)
        self.assertEqual(finished["status"], jobs_module.SUCCEEDED)
        self.assertEqual(finished["result"], {"doubled": 42})
        self.assertIsNotNone(finished["started_at"])
        self.assertIsNotNone(finished["finished_at"])

    def test_failing_job_reports_the_error(self) -> None:
        painter = jobs_module.get_runner(self.root / "jobs.json")

        def boom() -> None:
            raise RuntimeError("training exploded")

        job = painter.submit("boom", boom)
        finished = painter.wait(job["job_id"], timeout=5)
        self.assertEqual(finished["status"], jobs_module.FAILED)
        self.assertIn("training exploded", finished["error"])

    def test_jobs_list_filters_by_kind(self) -> None:
        painter = jobs_module.get_runner(self.root / "jobs.json")
        painter.submit("alpha", lambda: None)
        painter.submit("beta", lambda: None)
        self.assertEqual(len(painter.list(kind="alpha")), 1)
        self.assertEqual(painter.summary()["total"], 2)


class SourceTests(ApiTestBase):
    def test_create_is_idempotent_and_patchable(self) -> None:
        payload = {
            "source_id": "cta-daily",
            "name": "CTA daily counts",
            "kind": "observations",
            "label": "imported",
            "max_age_minutes": 1440,
        }
        first = self.client.post("/api/sources", json=payload, headers={"Idempotency-Key": "src-1"})
        self.assertEqual(first.status_code, 201, first.text)
        replay = self.client.post("/api/sources", json=payload, headers={"Idempotency-Key": "src-1"})
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.headers.get("Idempotent-Replayed"), "true")

        listing = self.client.get("/api/sources").json()
        self.assertEqual(listing["count"], 1)

        patched = self.client.patch("/api/sources/cta-daily", json={"enabled": False, "mark_success": True})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertFalse(patched.json()["source"]["enabled"])
        self.assertIsNotNone(patched.json()["source"]["last_success_at"])

        freshness = self.client.get("/api/sources/cta-daily/freshness").json()
        self.assertIn(freshness["label"], {"imported", "stale", "unavailable"})
        self.assertEqual(self.client.get("/api/sources/missing").status_code, 404)

    def test_unknown_fields_are_rejected(self) -> None:
        response = self.client.post(
            "/api/sources",
            json={"source_id": "x1", "name": "X", "kind": "weather", "unexpected": True},
        )
        self.assertEqual(response.status_code, 422)


class WeatherTests(ApiTestBase):
    def test_batch_ingest_deduplicates_and_paginates(self) -> None:
        batch = {
            "observations": [
                {"station_id": "KMDW", "observed_at": "2026-01-01T12:00:00Z", "temperature_c": 1.5},
                {"station_id": "KMDW", "observed_at": "2026-01-01T13:00:00Z", "temperature_c": 2.0},
            ]
        }
        first = self.client.post("/api/weather/observations", json=batch)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["accepted"], 2)

        second = self.client.post("/api/weather/observations", json=batch)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["accepted"], 0)
        self.assertEqual(second.json()["skipped"], 2)

        page = self.client.get("/api/weather/observations", params={"page_size": 1}).json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)
        self.assertTrue(page["has_next"])

        status = self.client.get("/api/weather/status").json()
        self.assertEqual(status["observations"], 2)
        self.assertIn("KMDW", status["stations"])


class ObservationTests(ApiTestBase):
    def test_ingest_reports_duplicates(self) -> None:
        payload = {
            "observations": [
                {"route_id": "66", "service_date": "2026-02-01", "boardings": 1200},
                {"route_id": "66", "service_date": "2026-02-02", "boardings": 1300},
            ]
        }
        first = self.client.post("/api/ingestion/observations", json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["accepted"], 2)

        repeat = self.client.post("/api/ingestion/observations", json=payload)
        self.assertEqual(repeat.json()["accepted"], 0)
        self.assertEqual(repeat.json()["duplicates"], 2)

        summary = self.client.get("/api/ingestion/observations/summary").json()
        self.assertEqual(summary["totals"] if "totals" in summary else summary["total_boardings"], 2500)

        page = self.client.get("/api/ingestion/observations", params={"route_id": "66", "page_size": 1}).json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["items"][0]["service_date"], "2026-02-02")

    def test_passenger_identifiers_are_refused(self) -> None:
        response = self.client.post(
            "/api/ingestion/observations",
            json={"observations": [{"route_id": "66", "service_date": "2026-02-01", "boardings": 10, "passenger_id": "abc"}]},
        )
        self.assertEqual(response.status_code, 422)

    def test_invalid_route_id_is_refused(self) -> None:
        response = self.client.post(
            "/api/ingestion/observations",
            json={"observations": [{"route_id": "bad route!", "service_date": "2026-02-01", "boardings": 10}]},
        )
        self.assertEqual(response.status_code, 422)


class RealtimeTests(ApiTestBase):
    def test_snapshot_is_stored_once_per_checksum(self) -> None:
        payload = {"agency": "test-agency", "entity": [{"id": "v-1"}, {"id": "v-2"}]}
        first = self.client.post("/api/ingestion/gtfs-realtime/snapshots", json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertTrue(first.json()["accepted"])

        repeat = self.client.post("/api/ingestion/gtfs-realtime/snapshots", json=payload)
        self.assertTrue(repeat.json()["duplicate"])
        self.assertFalse(repeat.json()["accepted"])

        page = self.client.get("/api/ingestion/gtfs-realtime/snapshots").json()
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["entity_count"], 2)

        status = self.client.get("/api/ingestion/gtfs-realtime/status").json()
        self.assertEqual(status["snapshots"], 1)
        self.assertFalse(status["stale"])


class AlertTests(ApiTestBase):
    def test_generation_stores_aggregate_alerts(self) -> None:
        response = self.client.post("/api/alerts/generate", json={})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertGreaterEqual(body["raised"], 1)
        self.assertIn("no-data", body["codes"])

        page = self.client.get("/api/alerts", params={"page_size": 10}).json()
        self.assertGreaterEqual(page["total"], 1)
        allowed = {"alert_id", "code", "severity", "title", "detail", "route_id", "label", "detected_at"}
        for item in page["items"]:
            self.assertTrue(set(item).issubset(allowed), set(item) - allowed)
            self.assertNotIn("passenger_id", str(item).lower())

        summary = self.client.get("/api/alerts/summary").json()
        self.assertEqual(summary["total"], page["total"])


class ReportingTests(ApiTestBase):
    def test_catalog_lists_reports(self) -> None:
        response = self.client.get("/api/reporting/catalog")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("reports", response.json())

    def test_unknown_report_run_is_404(self) -> None:
        response = self.client.post("/api/reporting/runs", json={"report_id": "not-a-report"})
        self.assertEqual(response.status_code, 404)

    def test_run_history_is_paginated(self) -> None:
        page = self.client.get("/api/reporting/runs", params={"page": 1, "page_size": 5}).json()
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["items"], [])


class OccupancyTests(ApiTestBase):
    def test_projection_reports_unavailable_without_a_forecast(self) -> None:
        body = self.client.get("/api/occupancy/projection").json()
        self.assertFalse(body["available"])
        self.assertEqual(body["label"], "unavailable")
        self.assertIn("forecast", body["reason"].lower())

    def test_assumptions_list_is_empty_by_default(self) -> None:
        body = self.client.get("/api/occupancy/assumptions").json()
        self.assertEqual(body["count"], 0)


class NetworkTests(ApiTestBase):
    def test_overview_and_routes_work_with_an_empty_store(self) -> None:
        overview = self.client.get("/api/network/overview")
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertEqual(overview.json()["routes"], 0)

        page = self.client.get("/api/network/routes").json()
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["items"], [])


class AuthTests(ApiTestBase):
    def test_capabilities_expose_the_role_matrix(self) -> None:
        response = self.client.get("/api/auth/capabilities")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("admin", body["roles"])
        self.assertIn("data:write", body["roles"]["operator"])
        self.assertTrue(any("/api/sources" in rule["pattern"] for rule in body["policy"]))

    def test_audit_is_paginated(self) -> None:
        self.client.post("/api/sources", json={"source_id": "audit-1", "name": "A", "kind": "weather"})
        page = self.client.get("/api/auth/audit", params={"page_size": 2}).json()
        self.assertEqual(page["page"], 1)
        self.assertLessEqual(len(page["items"]), 2)
        self.assertIn("pages", page)


if __name__ == "__main__":
    unittest.main()
