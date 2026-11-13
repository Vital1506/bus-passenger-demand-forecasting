"""Unit and integration tests for Phase 4 APC / AFC passenger data ingestion pipeline."""

from __future__ import annotations

import pytest
import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.db.session import init_db
from src.db.models import DailyPassengerRecord, TripPassengerObservation, StopPassengerObservation, ImportJobRecord
from src.passenger_data import parse_and_validate_apc_data, ingest_apc_dataset


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        yield session


def test_apc_validation_valid_csv():
    csv_data = """service_date,route_id,trip_id,stop_id,stop_sequence,boardings,alightings,vehicle_id,observation_timestamp
2026-11-01,R10,T-01,ST-01,1,12,0,V-100,2026-11-01T08:00:00Z
2026-11-01,R10,T-01,ST-02,2,8,4,V-100,2026-11-01T08:05:00Z
2026-11-01,R10,T-01,ST-03,3,2,16,V-100,2026-11-01T08:12:00Z
2026-11-01,R20,T-02,ST-10,1,25,0,V-200,2026-11-01T08:15:00Z
"""
    clean_df, val = parse_and_validate_apc_data(csv_data, agency_id="CTA")
    assert val.is_valid is True
    assert val.valid_records == 4
    assert val.duplicate_records == 0
    assert val.has_timestamps is True
    assert val.has_stop_level_data is True
    assert val.has_trip_level_data is True
    assert "R10" in val.routes_detected
    assert "R20" in val.routes_detected


def test_apc_validation_rejects_missing_required_columns():
    csv_data = """route_id,trip_id,passengers
R10,T-01,15
"""
    clean_df, val = parse_and_validate_apc_data(csv_data)
    assert val.is_valid is False
    assert any("Missing required" in err for err in val.error_samples)


def test_apc_validation_rejects_negative_and_nan_boardings():
    csv_data = """service_date,route_id,boardings
2026-11-01,R10,15
2026-11-02,R10,-5
2026-11-03,R10,NaN
2026-11-04,R10,30
"""
    clean_df, val = parse_and_validate_apc_data(csv_data)
    assert val.total_records == 4
    assert val.valid_records == 2  # Only 15 and 30 are valid
    assert val.rejected_records == 2


def test_apc_duplicate_detection():
    csv_data = """service_date,route_id,boardings
2026-11-01,R10,15
2026-11-01,R10,25
"""
    clean_df, val = parse_and_validate_apc_data(csv_data)
    assert val.total_records == 2
    assert val.duplicate_records == 1
    assert val.valid_records == 1
    assert clean_df["boardings"].iloc[0] == 25  # Keep latest


def test_apc_idempotent_ingestion_does_not_multiply_counts(test_db):
    csv_data = """service_date,route_id,trip_id,stop_id,stop_sequence,boardings,alightings,vehicle_id
2026-11-01,R10,T-01,ST-01,1,10,0,V-100
2026-11-01,R10,T-01,ST-02,2,15,5,V-100
"""
    clean_df, _ = parse_and_validate_apc_data(csv_data, agency_id="CTA")
    res1 = ingest_apc_dataset(test_db, clean_df, agency_id="CTA", source_filename="batch1.csv")

    assert res1["status"] == "COMPLETED"
    assert res1["daily_records"]["inserted"] == 1
    assert res1["daily_records"]["updated"] == 0

    # Total daily passengers for R10 on 2026-11-01 should be 10 + 15 = 25
    daily_row = test_db.execute(select(DailyPassengerRecord).where(DailyPassengerRecord.route_id == "R10")).scalar_one()
    assert daily_row.passengers == 25.0

    # Repeat exact same ingestion
    res2 = ingest_apc_dataset(test_db, clean_df, agency_id="CTA", source_filename="batch1_reupload.csv")
    assert res2["status"] == "COMPLETED"
    assert res2["daily_records"]["inserted"] == 0
    assert res2["daily_records"]["updated"] == 1  # Updated, NOT duplicated

    # Check daily row count is still 1 and passengers remains 25.0 (NEVER 50.0)
    rows = test_db.execute(select(DailyPassengerRecord).where(DailyPassengerRecord.route_id == "R10")).scalars().all()
    assert len(rows) == 1
    assert rows[0].passengers == 25.0

    # Verify import job records
    jobs = test_db.execute(select(ImportJobRecord)).scalars().all()
    assert len(jobs) == 2
    assert all(j.status == "COMPLETED" for j in jobs)
