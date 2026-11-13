"""Unit and integration tests for Phase 1 Database Foundation."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.db.migrate_files_to_db import compute_sha256, run_migration
from src.db.models import (
    Base,
    DailyPassengerRecord,
    DataAuditEvent,
    MonthlyImportEvent,
    MonthlyPassengerRecord,
    SourceMetadata,
)
from src.db.repository import (
    get_daily_counts_df,
    get_daily_counts_summary,
    get_monthly_observations,
    get_monthly_route_history,
    get_monthly_routes_summary,
    get_monthly_store_status,
    get_recent_audit_events,
    read_source_metadata,
    record_audit_event,
    record_monthly_import_event,
    replace_all_daily_counts,
    upsert_daily_counts,
    upsert_monthly_records,
    write_source_metadata,
)
from src.db.session import (
    check_db_health,
    check_storage_health,
    get_database_url,
    get_migration_status,
    init_db,
)


@pytest.fixture
def memory_db():
    """In-memory SQLite database for isolated unit tests."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        yield session, engine


def test_database_url_normalization(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@host:5432/db")
    url = get_database_url()
    assert url == "postgresql://user:pass@host:5432/db"

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host:5432/db")
    assert get_database_url() == "postgresql://user:pass@host:5432/db"


def test_health_checks(memory_db):
    _, engine = memory_db
    health = check_db_health(engine)
    assert health["status"] == "healthy"
    assert health["connected"] is True
    assert health["dialect"] == "sqlite"
    assert isinstance(health["latency_ms"], float)

    storage = check_storage_health()
    assert "data_directory" in storage
    assert "writable" in storage
    assert "free_mb" in storage

    mig_status = get_migration_status(engine)
    assert mig_status["initialized"] is True
    assert "daily_passenger_records" in mig_status["tables"]
    assert "monthly_passenger_records" in mig_status["tables"]


def test_daily_passenger_upsert_and_retrieval(memory_db):
    session, _ = memory_db
    records = [
        {"service_date": "2026-11-01", "route_id": "R10", "passengers": 120.0},
        {"service_date": "2026-11-02", "route_id": "R10", "passengers": 130.0},
        {"service_date": "2026-11-01", "route_id": "R20", "passengers": 200.0},
    ]

    inserted, updated = upsert_daily_counts(session, records, agency_id="default")
    assert inserted == 3
    assert updated == 0
    session.commit()

    # Re-upsert with one updated count and one new count (idempotence)
    next_batch = [
        {"service_date": "2026-11-02", "route_id": "R10", "passengers": 145.0},
        {"service_date": "2026-11-03", "route_id": "R10", "passengers": 150.0},
    ]
    inserted2, updated2 = upsert_daily_counts(session, next_batch, agency_id="default")
    assert inserted2 == 1
    assert updated2 == 1
    session.commit()

    df = get_daily_counts_df(session, agency_id="default")
    assert len(df) == 4
    assert set(df["route_id"]) == {"R10", "R20"}

    # Verify updated value
    r10_day2 = df.loc[(df["route_id"] == "R10") & (df["service_date"] == pd.Timestamp("2026-11-02")), "passengers"].iloc[0]
    assert r10_day2 == 145.0

    summary = get_daily_counts_summary(session, agency_id="default")
    assert summary["rows"] == 4
    assert summary["route_count"] == 2
    assert summary["earliest_date"] == "2026-11-01"
    assert summary["latest_date"] == "2026-11-03"


def test_replace_all_daily_counts(memory_db):
    session, _ = memory_db
    initial = [
        {"service_date": "2026-11-01", "route_id": "R1", "passengers": 50.0},
    ]
    upsert_daily_counts(session, initial)
    session.commit()

    replacement = [
        {"service_date": "2026-11-05", "route_id": "R2", "passengers": 80.0},
        {"service_date": "2026-11-06", "route_id": "R2", "passengers": 90.0},
    ]
    count = replace_all_daily_counts(session, replacement, agency_id="default")
    session.commit()
    assert count == 2

    df = get_daily_counts_df(session, agency_id="default")
    assert len(df) == 2
    assert set(df["route_id"]) == {"R2"}


def test_monthly_records_upsert_and_history(memory_db):
    session, _ = memory_db
    records = [
        {"month_start": "2024-01-01", "route_id": "A10", "route_name": "Route Alpha", "boardings": "100"},
        {"month_start": "2024-03-01", "route_id": "A10", "route_name": "Route Alpha Ext", "boardings": "150"},
        {"month_start": "2024-02-01", "route_id": "B20", "route_name": "Route Beta", "boardings": "250"},
    ]

    added, updated, unchanged = upsert_monthly_records(session, records, agency_id="cta")
    assert added == 3
    assert updated == 0
    assert unchanged == 0
    session.commit()

    # Re-upload same records: should report unchanged
    added2, updated2, unchanged2 = upsert_monthly_records(session, records, agency_id="cta")
    assert added2 == 0
    assert updated2 == 0
    assert unchanged2 == 3
    session.commit()

    # Search routes
    search = get_monthly_routes_summary(session, q="alpha", limit=10, agency_id="cta")
    assert search["total_matches"] == 1
    assert search["routes"][0]["route_id"] == "A10"
    assert search["routes"][0]["observed_months"] == 2
    assert search["routes"][0]["latest_boardings"] == "150"

    # Route history
    history = get_monthly_route_history(session, "A10", agency_id="cta")
    assert history is not None
    assert history["route_id"] == "A10"
    assert history["observed_months"] == 2
    assert history["missing_months"] == 1  # February is missing
    assert len(history["records"]) == 2
    assert history["records"][0]["month_start"] == "2024-01-01"
    assert history["records"][1]["month_start"] == "2024-03-01"

    status = get_monthly_store_status(session, agency_id="cta")
    assert status["storage_exists"] is True
    assert status["record_count"] == 3
    assert status["route_count"] == 2


def test_audit_and_metadata_repository(memory_db):
    session, _ = memory_db
    write_source_metadata(session, {"source": "unit_test", "record_count": 42})
    session.commit()

    meta = read_source_metadata(session)
    assert meta["source"] == "unit_test"
    assert meta["record_count"] == 42

    record_audit_event(session, action="test_action", source_system="pytest", record_count=10, route_count=2)
    session.commit()

    events = get_recent_audit_events(session, limit=5)
    assert len(events) == 1
    assert events[0]["action"] == "test_action"
    assert events[0]["record_count"] == 10


def test_full_file_to_db_migration(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monthly_dir = data_dir / "monthly"
    monthly_dir.mkdir(parents=True)

    # 1. Create source files
    daily_csv = data_dir / "daily_ridership.csv"
    daily_csv.write_text("service_date,route_id,passengers\n2026-11-01,R1,100\n2026-11-02,R1,110\n", encoding="utf-8")

    monthly_json = monthly_dir / "cta_monthly_ridership.json"
    monthly_json.write_text(json.dumps({
        "schema_version": 1,
        "observations": [
            {"month_start": "2024-01-01", "route_id": "CTA1", "route_name": "Halsted", "boardings": "5000"}
        ],
        "imports": [
            {
                "original_filename": "test.csv",
                "uploaded_at_utc": "2026-11-13T10:00:00Z",
                "source_info": "test",
                "sha256": "abcdef",
                "incoming_records": 1,
                "added_records": 1,
                "updated_records": 0,
                "unchanged_records": 0,
                "final_record_count": 1,
            }
        ]
    }), encoding="utf-8")

    meta_json = data_dir / "data_source.json"
    meta_json.write_text(json.dumps({"source": "company upload", "record_count": 2}), encoding="utf-8")

    audit_jsonl = data_dir / "ingest_audit.jsonl"
    audit_jsonl.write_text(json.dumps({"action": "upload", "source_system": "manual_csv", "record_count": 2, "route_count": 1}) + "\n", encoding="utf-8")

    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    # Test dry run
    dry_rep = run_migration(data_dir=data_dir, dry_run=True, backup=False)
    assert dry_rep["success"] is True
    assert dry_rep["daily_migration"]["source_rows"] == 2
    assert dry_rep["monthly_migration"]["source_observations"] == 1
    assert not db_path.exists()

    # Test live migration
    live_rep = run_migration(data_dir=data_dir, dry_run=False, backup=True)
    assert live_rep["success"] is True
    assert live_rep["daily_migration"]["migrated_count"] == 2
    assert live_rep["monthly_migration"]["added"] == 1
    assert len(live_rep["backups"]) == 4

    # Verify in DB
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with Session(engine) as session:
        df = get_daily_counts_df(session)
        assert len(df) == 2
        assert df["passengers"].sum() == 210

        obs = get_monthly_observations(session)
        assert len(obs) == 1
        assert obs[0]["route_id"] == "CTA1"

        meta = read_source_metadata(session)
        assert meta["source"] == "company upload"

        audits = get_recent_audit_events(session)
        assert len(audits) == 1
        assert audits[0]["action"] == "upload"


def test_database_endpoints_and_handlers(tmp_path, monkeypatch):
    import json
    import src.app as app_mod

    db_path = tmp_path / "test_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("BUS_FORECAST_DATA_DIR", str(tmp_path / "app_data"))
    monkeypatch.setenv("BUS_FORECAST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BUS_FORECAST_API_KEY", "db-test-key")

    # 1. Health check
    h = app_mod.health()
    assert h["status"] == "ok"

    # 2. Readiness check
    ready = app_mod.readiness()
    assert ready.status_code in (200, 503)
    body = json.loads(ready.body.decode("utf-8"))
    assert "database" in body
    assert "storage" in body

    # 3. Authenticated DB health
    with pytest.raises(Exception):
        app_mod.authorize(None)

    app_mod.authorize("Bearer db-test-key")

    db_h = app_mod.get_db_health()
    assert db_h["status"] == "healthy"
    assert db_h["connected"] is True

    # 4. Storage health
    st_h = app_mod.get_storage_health()
    assert st_h["writable"] is True

    # 5. DB migrations status
    app_mod.init_db()
    mig = app_mod.get_db_migrations()
    assert "daily_passenger_records" in mig["tables"]
    assert "monthly_passenger_records" in mig["tables"]

    # 6. Verify routes are registered in app
    route_paths = {route.path for route in app_mod.app.routes}
    assert "/api/ready" in route_paths
    assert "/api/db/health" in route_paths
    assert "/api/storage/health" in route_paths
    assert "/api/db/migrations" in route_paths

