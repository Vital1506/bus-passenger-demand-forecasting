"""End-to-end system integration benchmarks and high-concurrency stress tests."""

from __future__ import annotations

import concurrent.futures
import time
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.session import init_db
from src.gtfs_importer import import_gtfs_feed
from src.hybrid_forecasting import build_intraday_route_forecast
from src.operations_engine import OperationsDecisionEngine
from src.realtime.models import OccupancyStatus, RealtimeFeedMessage, VehiclePosition, VehicleStatus
from src.realtime.service import RealtimeService, get_realtime_service
from tests.test_gtfs import create_sample_gtfs_zip


def test_concurrent_telemetry_ingestion_stress():
    """Verify thread-safe concurrent telemetry ingestion of 500 vehicles across 10 threads."""
    rt_service = get_realtime_service()
    rt_service.store.clear()
    now = int(time.time())

    num_threads = 10
    vehicles_per_thread = 50
    total_expected = num_threads * vehicles_per_thread

    def worker_ingest(worker_id: int):
        vehs = []
        for i in range(vehicles_per_thread):
            vid = f"VEH-{worker_id}-{i}"
            vehs.append(
                VehiclePosition(
                    vehicle_id=vid,
                    route_id=f"R-{worker_id}",
                    latitude=41.80 + (i * 0.001),
                    longitude=-87.60 - (worker_id * 0.01),
                    speed_mps=10.0,
                    current_status=VehicleStatus.IN_TRANSIT_TO,
                    occupancy_status=OccupancyStatus.MANY_SEATS_AVAILABLE,
                    timestamp=now,
                )
            )
        msg = RealtimeFeedMessage(feed_timestamp=now, vehicles=vehs)
        return rt_service.store.ingest_feed(msg)

    start_time = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker_ingest, t) for t in range(num_threads)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    duration = time.perf_counter() - start_time

    assert len(results) == num_threads
    all_active = rt_service.get_vehicles()
    assert len(all_active) == total_expected
    # Ensure high throughput: 500 vehicles ingested concurrently in less than 2.0 seconds
    assert duration < 2.0


def test_full_e2e_platform_pipeline_benchmark():
    """Benchmark end-to-end flow: GTFS static schedule -> GTFS-RT feed -> hybrid forecast -> operations dispatch."""
    start_time = time.perf_counter()

    # 1. Setup in-memory DB and import GTFS Schedule
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        zip_bytes = create_sample_gtfs_zip()
        summary = import_gtfs_feed(session, zip_bytes, feed_id="bench_feed_01", agency_id="CTA")
        session.commit()
        assert summary.routes_count == 2

    # 2. Ingest GTFS-RT feed with bunched vehicles
    rt_service = get_realtime_service()
    rt_service.store.clear()
    now = int(time.time())

    v1 = VehiclePosition(
        vehicle_id="BENCH-V1",
        route_id="1",
        latitude=41.8800,
        longitude=-87.6200,
        current_stop_sequence=4,
        occupancy_status=OccupancyStatus.MANY_SEATS_AVAILABLE,
        timestamp=now,
    )
    v2 = VehiclePosition(
        vehicle_id="BENCH-V2",
        route_id="1",
        latitude=41.8825,  # bunched
        longitude=-87.6200,
        current_stop_sequence=5,
        occupancy_status=OccupancyStatus.FULL,
        timestamp=now,
    )

    rt_service.store.ingest_feed(RealtimeFeedMessage(feed_timestamp=now, vehicles=[v1, v2]))

    # 3. Compute real-time hybrid forecast
    forecast = build_intraday_route_forecast("1", daily_baseline_passengers=4500.0, current_hour=9)
    assert forecast.is_realtime_calibrated is True
    assert forecast.active_vehicles_count == 2
    assert len(forecast.hourly_profile) == 24

    # 4. Run operations decision engine
    ops_engine = OperationsDecisionEngine()
    anomalies, recs, metrics = ops_engine.evaluate_fleet()

    assert metrics.bunching_events_count >= 1
    assert any(r.action_type == "HOLD" for r in recs)

    total_pipeline_time = time.perf_counter() - start_time
    # Complete end-to-end execution must complete well under 1.5 seconds
    assert total_pipeline_time < 1.5
