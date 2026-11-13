"""Unit tests for hybrid intraday demand forecasting engine."""

from __future__ import annotations

import pytest

from src.hybrid_forecasting import (
    build_intraday_route_forecast,
    build_network_intraday_curve,
    occupancy_to_passengers,
    OccupancyStatus,
)
from src.realtime.service import get_realtime_service


def test_occupancy_to_passengers():
    cap = 50
    assert occupancy_to_passengers(OccupancyStatus.EMPTY, capacity=cap) <= 5
    assert occupancy_to_passengers(OccupancyStatus.MANY_SEATS_AVAILABLE, capacity=cap) == 12
    assert occupancy_to_passengers(OccupancyStatus.STANDING_ROOM_ONLY, capacity=cap) == 42
    assert occupancy_to_passengers(OccupancyStatus.FULL, capacity=cap) == 51

    # Explicit percentage
    assert occupancy_to_passengers(OccupancyStatus.NO_DATA_AVAILABLE, percentage=80, capacity=cap) == 40
    assert occupancy_to_passengers(OccupancyStatus.NO_DATA_AVAILABLE, percentage=10, capacity=cap) == 5


def test_intraday_forecast_fallback_without_telemetry():
    rt = get_realtime_service()
    rt.store.clear()

    forecast = build_intraday_route_forecast("ROUTE-X", daily_baseline_passengers=3000.0, current_hour=10)

    assert forecast.route_id == "ROUTE-X"
    assert forecast.is_realtime_calibrated is False
    assert forecast.active_vehicles_count == 0
    assert forecast.current_onboard_passengers == 0
    assert len(forecast.hourly_profile) == 24
    assert forecast.next_1h_predicted_boardings > 0
    assert forecast.next_4h_predicted_boardings > forecast.next_1h_predicted_boardings

    # Check bounds
    for h in forecast.hourly_profile:
        assert h["lower_90"] <= h["predicted_boardings"] <= h["upper_90"]


def test_intraday_forecast_calibrated_with_telemetry():
    rt = get_realtime_service()
    rt.generate_synthetic_live_snapshot(agency_id="CTA")

    forecast = build_intraday_route_forecast("1", daily_baseline_passengers=5000.0, current_hour=8)

    assert forecast.route_id == "1"
    assert forecast.is_realtime_calibrated is True
    assert forecast.active_vehicles_count >= 2
    assert forecast.current_onboard_passengers > 0
    assert forecast.crowding_risk_level in {"LOW", "MODERATE", "HIGH", "CRITICAL"}
    assert len(forecast.hourly_profile) == 24


def test_network_intraday_curve():
    curve_data = build_network_intraday_curve(current_hour=14)

    assert curve_data["current_hour"] == 14
    assert len(curve_data["hourly_curve"]) == 24
    assert curve_data["network_daily_projected"] == 50000
    assert curve_data["total_active_vehicles"] >= 0


def test_hybrid_forecasting_api_endpoints():
    import src.app as app_mod

    # 1. Routes registered
    paths = {r.path for r in app_mod.app.routes}
    assert "/api/forecast/realtime/{route_id}" in paths
    assert "/api/forecast/intraday" in paths

    # 2. Query route intraday forecast
    r_fc = app_mod.get_route_realtime_forecast("1", baseline_passengers=3200.0)
    assert r_fc["route_id"] == "1"
    assert len(r_fc["hourly_profile"]) == 24
    assert r_fc["daily_baseline_boardings"] == 3200.0

    # 3. Query network intraday curve
    net_curve = app_mod.get_intraday_network_curve()
    assert len(net_curve["hourly_curve"]) == 24
    assert "network_daily_projected" in net_curve

