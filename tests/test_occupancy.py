"""Unit tests for Phase 6 Passenger Occupancy and Capacity Intelligence Engine."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.session import init_db
from src.db.models import StopPassengerObservation, VehicleRecord
from src.occupancy_engine import compute_trip_occupancy, compute_route_capacity_profile


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        yield session


def test_occupancy_conservation_equation(test_db):
    # Register vehicle with capacity = 50
    v = VehicleRecord(
        agency_id="CTA",
        vehicle_id="BUS-101",
        seated_capacity=35,
        standing_capacity=15,
        total_capacity=50,
        is_active=True,
    )
    test_db.add(v)

    # Insert 4 stops along TRIP-1
    # Stop 1: +20 on, 0 off -> Load = 20
    # Stop 2: +15 on, 5 off -> Load = 30
    # Stop 3: +10 on, 15 off -> Load = 25
    # Stop 4: +0 on, 25 off -> Load = 0
    now = datetime.now(timezone.utc)
    stops = [
        StopPassengerObservation(
            agency_id="CTA", route_id="R1", trip_id="TRIP-1", stop_id="S1", stop_sequence=1,
            boardings=20, alightings=0, vehicle_id="BUS-101", observation_timestamp_utc=now,
        ),
        StopPassengerObservation(
            agency_id="CTA", route_id="R1", trip_id="TRIP-1", stop_id="S2", stop_sequence=2,
            boardings=15, alightings=5, vehicle_id="BUS-101", observation_timestamp_utc=now,
        ),
        StopPassengerObservation(
            agency_id="CTA", route_id="R1", trip_id="TRIP-1", stop_id="S3", stop_sequence=3,
            boardings=10, alightings=15, vehicle_id="BUS-101", observation_timestamp_utc=now,
        ),
        StopPassengerObservation(
            agency_id="CTA", route_id="R1", trip_id="TRIP-1", stop_id="S4", stop_sequence=4,
            boardings=0, alightings=25, vehicle_id="BUS-101", observation_timestamp_utc=now,
        ),
    ]
    test_db.add_all(stops)
    test_db.commit()

    profile = compute_trip_occupancy(test_db, "TRIP-1", agency_id="CTA")
    assert profile is not None
    assert profile.trip_id == "TRIP-1"
    assert profile.total_boardings == 45
    assert profile.total_alightings == 45
    assert profile.peak_occupancy == 30
    assert profile.vehicle_capacity == 50
    assert profile.peak_utilization_pct == 60.0  # 30 / 50 * 100
    assert len(profile.overcapacity_events) == 0
    assert profile.confidence_status == "VERIFIED_APC"

    loads = [s.onboard_load for s in profile.stops]
    assert loads == [20, 30, 25, 0]


def test_overcapacity_detection(test_db):
    # Vehicle capacity = 40
    v = VehicleRecord(
        agency_id="CTA",
        vehicle_id="BUS-MINI",
        seated_capacity=30,
        standing_capacity=10,
        total_capacity=40,
        is_active=True,
    )
    test_db.add(v)

    now = datetime.now(timezone.utc)
    stops = [
        StopPassengerObservation(
            agency_id="CTA", route_id="R1", trip_id="TRIP-OVER", stop_id="S1", stop_sequence=1,
            boardings=48, alightings=0, vehicle_id="BUS-MINI", observation_timestamp_utc=now,
        ),
        StopPassengerObservation(
            agency_id="CTA", route_id="R1", trip_id="TRIP-OVER", stop_id="S2", stop_sequence=2,
            boardings=0, alightings=48, vehicle_id="BUS-MINI", observation_timestamp_utc=now,
        ),
    ]
    test_db.add_all(stops)
    test_db.commit()

    profile = compute_trip_occupancy(test_db, "TRIP-OVER", agency_id="CTA")
    assert profile is not None
    assert profile.peak_occupancy == 48
    assert len(profile.overcapacity_events) == 1
    assert profile.overcapacity_events[0]["stop_id"] == "S1"
    assert profile.overcapacity_events[0]["excess_passengers"] == 8
    assert profile.overcapacity_events[0]["utilization_pct"] == 120.0


def test_negative_load_anomaly_clamped_with_disclosure(test_db):
    now = datetime.now(timezone.utc)
    # 5 board, but 10 alight (sensor counting error in raw feed)
    stops = [
        StopPassengerObservation(
            agency_id="CTA", route_id="R1", trip_id="TRIP-ERR", stop_id="S1", stop_sequence=1,
            boardings=5, alightings=10, vehicle_id=None, observation_timestamp_utc=now,
        ),
    ]
    test_db.add_all(stops)
    test_db.commit()

    profile = compute_trip_occupancy(test_db, "TRIP-ERR", agency_id="CTA")
    assert profile is not None
    assert profile.stops[0].onboard_load == 0  # Clamped to non-negative
    assert len(profile.anomalies_detected) == 1
    assert "Negative load" in profile.anomalies_detected[0]


def test_route_capacity_profile_empty_and_populated(test_db):
    empty_res = compute_route_capacity_profile(test_db, "NONEXISTENT", agency_id="CTA")
    assert empty_res["status"] == "NO_APC_DATA"
    assert empty_res["trips_evaluated"] == 0
