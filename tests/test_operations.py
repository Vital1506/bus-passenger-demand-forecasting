"""Unit tests for transit operations and dispatch decision support engine."""

from __future__ import annotations

import time
import pytest

from src.operations_engine import OperationsDecisionEngine, get_operations_engine
from src.realtime.models import (
    OccupancyStatus,
    RealtimeFeedMessage,
    VehiclePosition,
    VehicleStatus,
    TripUpdate,
)
from src.realtime.service import get_realtime_service


def test_bunching_detection_and_holding_recommendation():
    engine = OperationsDecisionEngine()
    rt = get_realtime_service()
    rt.store.clear()
    now = int(time.time())

    # Two buses 250 meters apart on Route 1
    v1 = VehiclePosition(
        vehicle_id="BUS-A",
        route_id="1",
        latitude=41.8800,
        longitude=-87.6200,
        current_stop_sequence=5,
        stop_id="STOP-5",
        timestamp=now,
    )
    v2 = VehiclePosition(
        vehicle_id="BUS-B",
        route_id="1",
        latitude=41.8820,  # ~220 meters apart
        longitude=-87.6200,
        current_stop_sequence=6,
        stop_id="STOP-6",
        timestamp=now,
    )

    rt.store.ingest_feed(RealtimeFeedMessage(feed_timestamp=now, vehicles=[v1, v2]))

    anomalies, recs, metrics = engine.evaluate_fleet()

    assert metrics.bunching_events_count == 1
    assert any(a.anomaly_type == "BUNCHING" for a in anomalies)

    # Check holding recommendation was generated for trailing vehicle
    hold_rec = next(r for r in recs if r.action_type == "HOLD")
    assert hold_rec.route_id == "1"
    assert hold_rec.hold_duration_seconds is not None
    assert "Hold" in hold_rec.instruction


def test_excessive_gap_and_overcrowding_detection():
    engine = OperationsDecisionEngine()
    rt = get_realtime_service()
    rt.store.clear()
    now = int(time.time())

    # Two buses 8 km apart on Route 2, with one bus FULL
    v1 = VehiclePosition(
        vehicle_id="BUS-X",
        route_id="2",
        latitude=41.8000,
        longitude=-87.6000,
        current_stop_sequence=1,
        occupancy_status=OccupancyStatus.FULL,
        timestamp=now,
    )
    v2 = VehiclePosition(
        vehicle_id="BUS-Y",
        route_id="2",
        latitude=41.8720,  # ~8 km apart
        longitude=-87.6000,
        current_stop_sequence=15,
        occupancy_status=OccupancyStatus.EMPTY,
        timestamp=now,
    )

    rt.store.ingest_feed(RealtimeFeedMessage(feed_timestamp=now, vehicles=[v1, v2]))

    anomalies, recs, metrics = engine.evaluate_fleet()

    assert metrics.gap_events_count == 1
    assert metrics.overcrowding_count == 1

    gap_anom = next(a for a in anomalies if a.anomaly_type == "EXCESSIVE_GAP")
    assert gap_anom.route_id == "2"

    occ_anom = next(a for a in anomalies if a.anomaly_type == "OVERCROWDING")
    assert occ_anom.vehicle_ids == ["BUS-X"]


def test_operations_metrics_computation():
    engine = OperationsDecisionEngine()
    rt = get_realtime_service()
    rt.store.clear()
    now = int(time.time())

    rt.store.ingest_feed(
        RealtimeFeedMessage(
            feed_timestamp=now,
            vehicles=[
                VehiclePosition(vehicle_id="V1", route_id="10", latitude=41.85, longitude=-87.62, timestamp=now),
            ],
            trip_updates=[
                TripUpdate(trip_id="T1", route_id="10", delay=60, timestamp=now),   # On time
                TripUpdate(trip_id="T2", route_id="10", delay=180, timestamp=now),  # On time
                TripUpdate(trip_id="T3", route_id="10", delay=600, timestamp=now),  # Late
            ],
        )
    )

    _, _, metrics = engine.evaluate_fleet()

    assert metrics.active_fleet_count == 1
    assert metrics.on_time_performance_pct == pytest.approx(66.7, 0.1)
    assert metrics.excess_wait_time_minutes > 0
    assert 0.0 <= metrics.service_regularity_index <= 1.0


def test_operations_api_endpoints():
    import src.app as app_mod

    # 1. Routes registered
    paths = {r.path for r in app_mod.app.routes}
    assert "/api/operations/alerts" in paths
    assert "/api/operations/dispatch-recommendations" in paths
    assert "/api/operations/metrics" in paths

    # 2. Query alerts
    alerts = app_mod.get_operations_alerts()
    assert isinstance(alerts, list)

    # 3. Query recommendations
    recs = app_mod.get_dispatch_recommendations()
    assert isinstance(recs, list)

    # 4. Query metrics
    metrics = app_mod.get_operations_metrics()
    assert "excess_wait_time_minutes" in metrics
    assert "service_regularity_index" in metrics

