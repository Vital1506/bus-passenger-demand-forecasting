"""API checks for readiness, migration reporting and storage health."""

from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from src import app as app_module
from src import db
from src.db_admin import upgrade_database

KEY = "db-endpoint-test-key"


class ReadinessEndpointTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="db-endpoints-")
        self._saved = {
            name: os.environ.get(name)
            for name in ("DATABASE_URL", "BUS_FORECAST_REQUIRE_AUTH", "BUS_FORECAST_API_KEY")
        }
        os.environ["DATABASE_URL"] = f"sqlite:///{self.root}/api.db"
        os.environ["BUS_FORECAST_REQUIRE_AUTH"] = "true"
        os.environ["BUS_FORECAST_API_KEY"] = KEY
        db.reset_engine()
        self.client = TestClient(app_module.app)
        self.headers = {"Authorization": f"Bearer {KEY}"}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        db.reset_engine()

    def test_readiness_is_503_until_migrations_are_applied(self):
        response = self.client.get("/api/ready")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body["ready"])
        self.assertTrue(body["migrations"]["pending"])

    def test_readiness_is_200_after_migrations(self):
        upgrade_database()
        response = self.client.get("/api/ready")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ready"])
        self.assertTrue(body["database"]["reachable"])
        self.assertFalse(body["migrations"]["pending"])
        self.assertEqual(body["migrations"]["current"], body["migrations"]["head"])

    def test_migration_report_requires_the_operator_key(self):
        upgrade_database()
        self.assertEqual(self.client.get("/api/migrations").status_code, 401)
        response = self.client.get("/api/migrations", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("migrations", response.json())

    def test_storage_health_requires_the_operator_key(self):
        self.assertEqual(self.client.get("/api/storage/health").status_code, 401)
        response = self.client.get("/api/storage/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("writable", body)
        self.assertIn("artifacts", body)


if __name__ == "__main__":
    unittest.main()
