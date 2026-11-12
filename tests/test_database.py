"""Database layer checks: configuration, engine, migrations and storage health."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src import db
from src.db_admin import upgrade_database


class DatabaseConfigTests(unittest.TestCase):
    def test_postgres_urls_use_psycopg_driver(self):
        self.assertEqual(
            db.normalize_url("postgres://user:pass@host:5432/db"),
            "postgresql+psycopg://user:pass@host:5432/db",
        )
        self.assertEqual(
            db.normalize_url("postgresql://user:pass@host:5432/db"),
            "postgresql+psycopg://user:pass@host:5432/db",
        )
        self.assertEqual(db.normalize_url("sqlite:///tmp/x.db"), "sqlite:///tmp/x.db")

    def test_database_url_falls_back_to_sqlite_in_data_dir(self):
        saved = {name: os.environ.get(name) for name in ("DATABASE_URL", "BUS_FORECAST_DATA_DIR")}
        try:
            os.environ.pop("DATABASE_URL", None)
            root = Path(tempfile.mkdtemp(prefix="db-url-"))
            os.environ["BUS_FORECAST_DATA_DIR"] = str(root)
            url = db.database_url()
            self.assertTrue(url.startswith("sqlite:///"), url)
            self.assertIn("bus_forecast.db", url)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


class SchemaAndHealthTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="db-schema-"))
        self._saved = {name: os.environ.get(name) for name in ("DATABASE_URL", "BUS_FORECAST_DATA_DIR")}
        os.environ.pop("DATABASE_URL", None)
        os.environ["BUS_FORECAST_DATA_DIR"] = str(self.root)
        db.reset_engine()

    def tearDown(self):
        db.reset_engine()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        db.reset_engine()

    def test_migrations_apply_and_report_clean_state(self):
        before = db.migration_status()
        self.assertTrue(before["pending"], "a fresh database must report a pending migration")
        state = upgrade_database()
        self.assertEqual(state["current"], state["head"])
        self.assertFalse(state["pending"])

    def test_session_round_trip_is_transactional(self):
        upgrade_database()
        from sqlalchemy import select

        from src.schema import Agency

        with db.session_scope() as session:
            session.add(Agency(code="TEST", name="Test Agency"))
        with db.session_scope() as session:
            agencies = session.execute(select(Agency)).scalars().all()
            self.assertEqual([agency.code for agency in agencies], ["TEST"])
        with self.assertRaises(RuntimeError):
            with db.session_scope() as session:
                session.add(Agency(code="ROLLBACK", name="Should not persist"))
                raise RuntimeError("boom")
        with db.session_scope() as session:
            codes = {agency.code for agency in session.execute(select(Agency)).scalars()}
            self.assertNotIn("ROLLBACK", codes)

    def test_database_and_storage_health_report(self):
        upgrade_database()
        check = db.check_database()
        self.assertTrue(check["reachable"])
        self.assertEqual(check["dialect"], "sqlite")
        health = db.storage_health(self.root)
        self.assertTrue(health["writable"])
        self.assertTrue(health["exists"])
        self.assertIn("daily_csv", health["artifacts"])
        self.assertFalse(health["artifacts"]["daily_csv"]["present"])


if __name__ == "__main__":
    unittest.main()
