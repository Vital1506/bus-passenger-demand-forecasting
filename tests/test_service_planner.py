"""Unit tests for Phase 8 Intelligent Service Planning Simulator."""

from __future__ import annotations

import pytest

from src.service_planner import (
    SimulationResult,
    SimulationScenario,
    run_service_simulation,
)


def test_service_simulation_resolves_deficit():
    # 4 trips/hr with 55 pax capacity = 220 cap/hr. Demand = 280 pax/hr -> Deficit of 60 (127.3% util, CRITICAL)
    # Increase to 6 trips/hr = 330 cap/hr -> Surplus of 50 (84.8% util, HIGH/MODERATE)
    scenario = SimulationScenario(
        route_id="R10",
        forecast_hourly_demand=280.0,
        baseline_trips_per_hour=4.0,
        modified_trips_per_hour=6.0,
        vehicle_capacity=55,
        operating_hours=16,
    )
    res = run_service_simulation(scenario)
    assert res.crowding_risk_baseline == "CRITICAL"
    assert res.crowding_risk_modified == "MODERATE"
    assert res.baseline_balance_passengers < 0
    assert res.modified_balance_passengers > 0
    assert res.net_capacity_change_per_hour == 110  # 330 - 220
    assert res.daily_vehicle_trips_diff == 32.0  # +2 trips/hr * 16 hrs
    assert "resolves severe overcapacity" in res.operational_summary.lower()
    assert "decision support" in res.confidence_disclaimer.lower()


def test_service_simulation_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        SimulationScenario(route_id="R1", forecast_hourly_demand=100, baseline_trips_per_hour=0, modified_trips_per_hour=4)

    with pytest.raises(ValueError):
        SimulationScenario(route_id="R1", forecast_hourly_demand=-5, baseline_trips_per_hour=2, modified_trips_per_hour=4)


def test_service_simulation_serialization():
    scenario = SimulationScenario(
        route_id="R20",
        forecast_hourly_demand=150.0,
        baseline_trips_per_hour=4.0,
        modified_trips_per_hour=4.0,
    )
    res = run_service_simulation(scenario)
    d = res.to_dict()
    assert d["route_id"] == "R20"
    assert d["net_capacity_change_per_hour"] == 0
