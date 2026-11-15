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


def test_b1_hour_wraparound_all_24_hours():
    """Verify B1: hour wraparound correctly calibrates exactly 4 upcoming hours across midnight."""
    rt = get_realtime_service()
    rt.generate_synthetic_live_snapshot(agency_id="CTA")

    for now_h in range(24):
        fc = build_intraday_route_forecast("1", daily_baseline_passengers=4000.0, current_hour=now_h)
        calibrated_hours = [h["hour"] for h in fc.hourly_profile if h.get("is_calibrated")]
        expected_hours = [(now_h + offset) % 24 for offset in range(4)]
        assert sorted(calibrated_hours) == sorted(expected_hours), f"Failed for now_hour={now_h}"


def test_b2_agency_timezone_handling(monkeypatch):
    """Verify B2: timezone configuration reflects local time."""
    from src.hybrid_forecasting import get_agency_timezone, get_current_local_time

    monkeypatch.setenv("BUS_FORECAST_TIMEZONE_CTA", "America/Chicago")
    tz = get_agency_timezone("CTA")
    assert str(tz) == "America/Chicago"

    monkeypatch.setenv("BUS_FORECAST_TIMEZONE_MTA", "America/New_York")
    tz_mta = get_agency_timezone("MTA")
    assert str(tz_mta) == "America/New_York"

    # Default fallback
    monkeypatch.delenv("BUS_FORECAST_TIMEZONE_XYZ", raising=False)
    monkeypatch.setenv("BUS_FORECAST_TIMEZONE", "UTC")
    tz_def = get_agency_timezone("XYZ")
    assert str(tz_def) == "UTC"


def test_b4_interval_heuristics_labeling():
    """Verify B4: hourly forecast explicitly labels heuristic prediction intervals."""
    fc = build_intraday_route_forecast("ROUTE-1", daily_baseline_passengers=2000.0, current_hour=9)
    for h in fc.hourly_profile:
        assert h["interval_method"] == "heuristic_ratio"
        assert "lower_90_heuristic" in h
        assert "upper_90_heuristic" in h
        assert h["lower_90"] == h["lower_90_heuristic"]
        assert h["upper_90"] == h["upper_90_heuristic"]


def test_b3_realtime_forecast_baseline_linking(monkeypatch):
    """Verify B3: intraday forecast links to trained daily forecasts or raises 404."""
    import src.app as app_mod
    from fastapi import HTTPException
    from pathlib import Path

    # Explicit baseline works
    fc_explicit = app_mod.get_route_realtime_forecast("R10", baseline_passengers=1234.0)
    assert fc_explicit["daily_baseline_boardings"] == 1234.0

    # With missing file and cache, raises 404
    monkeypatch.setattr(app_mod, "FORECAST_PATH", Path("non_existent_path.csv"))
    monkeypatch.setattr(app_mod, "PUBLIC_DEMO_CACHE", None)
    with pytest.raises(HTTPException) as exc:
        app_mod.get_route_realtime_forecast("NON_EXISTENT_ROUTE")
    assert exc.value.status_code == 404


def test_b5_diurnal_profile_learning():
    """Verify B5: learned diurnal profile adapts weights from hourly APC records."""
    import pandas as pd
    from src.hybrid_forecasting import learn_diurnal_profile_from_hourly, build_intraday_route_forecast

    # Default fallback when no data
    weights, source = learn_diurnal_profile_from_hourly(None)
    assert source == "default_transit_standard"
    assert len(weights) == 24

    # With synthetic APC hourly data peaked at hour 17 (5 PM)
    hours = list(range(24))
    boardings = [10.0] * 24
    boardings[17] = 200.0  # Big evening rush
    df = pd.DataFrame({"route_id": ["99"] * 24, "hour": hours, "boardings": boardings})

    learned_weights, source = learn_diurnal_profile_from_hourly(df, route_id="99")
    assert source == "learned_apc"
    assert learned_weights[17] > 0.30  # Peak reflected

    fc = build_intraday_route_forecast("99", daily_baseline_passengers=1000.0, hourly_observations=df)
    assert fc.profile_source == "learned_apc"
    assert fc.to_dict()["profile_source"] == "learned_apc"



