"""Legacy file migration: dry-run, backup, idempotency and checksums."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select

from src import db
from src.db_admin import upgrade_database
from src.migrate_to_db import run_migration
from src.monthly_data import ingest_monthly_csv, monthly_store_path
from src.schema import DailyObservation, FeedImport, MonthlyObservation

DAILY_HEADER = "service_date,route_id,passengers"
MONTHLY_HEADER = "month_start,route_id,route_name,boardings"


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="migrate-db-"))
        self._saved = {name: os.environ.get(name) for name in ("DATABASE_URL", "BUS_FORECAST_DATA_DIR")}
        os.environ.pop("DATABASE_URL", None)
        os.environ["BUS_FORECAST_DATA_DIR"] = str(self.root)
        db.reset_engine()
        upgrade_database()

        daily = self.root / "daily_ridership.csv"
        daily.write_text(
            "\n".join(
                [
                    DAILY_HEADER,
                    "2026-01-01,R10,100",
                    "2026-01-02,R10,120",
                    "2026-01-01,R22,80",
                    "not-a-date,R22,10",
                    "2026-01-03,R22,-5",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        monthly_csv = (
            "\n".join(
                [
                    MONTHLY_HEADER,
                    "2026-01-01,R10,Route Ten,1000",
                    "2026-02-01,R10,Route Ten,1100",
                ]
            )
            + "\n"
        ).encode("utf-8")
        ingest_monthly_csv(
            monthly_store_path(self.root),
            self.root / "monthly" / "ingest_audit.jsonl",
            monthly_csv,
            file_name="test_monthly.csv",
        )

    def tearDown(self):
        db.reset_engine()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        db.reset_engine()

    def _counts(self):
        with db.session_scope() as session:
            return (
                session.scalar(select(func.count()).select_from(DailyObservation)),
                session.scalar(select(func.count()).select_from(MonthlyObservation)),
            )

    def test_dry_run_reports_without_writing(self):
        summary = run_migration(self.root, dry_run=True)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["would_import"], 5)
        self.assertEqual(self._counts(), (0, 0))
        self.assertTrue(any("invalid daily rows" in issue for issue in summary["issues"]))
        self.assertIsNotNone(summary["sources"]["daily_csv"]["sha256"])

    def test_migration_imports_and_is_idempotent(self):
        first = run_migration(self.root, make_backup=True)
        self.assertEqual(first["daily"]["inserted"], 3)
        self.assertEqual(first["monthly"]["inserted"], 2)
        self.assertEqual(self._counts(), (3, 2))
        self.assertTrue(first["backup"], "a backup copy must be created before writing")

        second = run_migration(self.root, make_backup=True)
        self.assertEqual(second["daily"]["inserted"], 0)
        self.assertEqual(second["daily"]["updated"], 3)
        self.assertEqual(second["monthly"]["inserted"], 0)
        self.assertEqual(second["monthly"]["updated"], 2)
        self.assertEqual(self._counts(), (3, 2))

        with db.session_scope() as session:
            imports = session.execute(select(FeedImport)).scalars().all()
            self.assertEqual(len(imports), 4)
            self.assertTrue(all(row.checksum for row in imports))

    def test_missing_sources_are_reported_not_fatal(self):
        empty = Path(tempfile.mkdtemp(prefix="migrate-empty-"))
        summary = run_migration(empty, make_backup=False)
        self.assertEqual(summary["daily"]["inserted"], 0)
        self.assertEqual(summary["monthly"]["inserted"], 0)
        self.assertTrue(any("not found" in issue for issue in summary["issues"]))
        json.dumps(summary, default=str)  # summary must stay serializable


if __name__ == "__main__":
    unittest.main()
