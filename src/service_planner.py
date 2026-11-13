"""Intelligent Service Planning Simulator.

Provides interactive 'what-if' decision support for transit operations and scheduling planners:
- Frequency adjustment simulation (increasing / reducing trips per hour)
- Fleet asset allocation (adding / removing vehicles)
- Vehicle capacity modeling (evaluating standard 40ft vs articulated 60ft buses)
- Demand-to-capacity shortfall and surplus calculation
- Peak-hour crowding mitigation analysis

Methodological Safeguards:
- Human-in-the-loop review: clearly notes recommendations are decision support, not automatic dispatch
- Transparent operational implications without unsubstantiated financial savings claims
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SimulationScenario:
    route_id: str
    forecast_hourly_demand: float
    baseline_trips_per_hour: float
    modified_trips_per_hour: float
    vehicle_capacity: int = 55
    operating_hours: int = 16

    def __post_init__(self):
        if self.baseline_trips_per_hour <= 0:
            raise ValueError("baseline_trips_per_hour must be positive")
        if self.modified_trips_per_hour <= 0:
            raise ValueError("modified_trips_per_hour must be positive")
        if self.vehicle_capacity <= 0:
            raise ValueError("vehicle_capacity must be positive")
        if self.forecast_hourly_demand < 0:
            raise ValueError("forecast_hourly_demand cannot be negative")


@dataclass
class SimulationResult:
    route_id: str
    vehicle_capacity: int
    forecast_hourly_demand: float
    baseline_trips_per_hour: float
    modified_trips_per_hour: float
    baseline_capacity_per_hour: int
    modified_capacity_per_hour: int
    baseline_utilization_pct: float
    modified_utilization_pct: float
    baseline_balance_passengers: float  # Positive = surplus, Negative = deficit
    modified_balance_passengers: float
    crowding_risk_baseline: str  # LOW, MODERATE, HIGH, CRITICAL
    crowding_risk_modified: str
    net_capacity_change_per_hour: int
    daily_vehicle_trips_diff: float
    operational_summary: str
    confidence_disclaimer: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "vehicle_capacity": self.vehicle_capacity,
            "forecast_hourly_demand": round(self.forecast_hourly_demand, 1),
            "baseline_trips_per_hour": round(self.baseline_trips_per_hour, 1),
            "modified_trips_per_hour": round(self.modified_trips_per_hour, 1),
            "baseline_capacity_per_hour": self.baseline_capacity_per_hour,
            "modified_capacity_per_hour": self.modified_capacity_per_hour,
            "baseline_utilization_pct": round(self.baseline_utilization_pct, 1),
            "modified_utilization_pct": round(self.modified_utilization_pct, 1),
            "baseline_balance_passengers": round(self.baseline_balance_passengers, 1),
            "modified_balance_passengers": round(self.modified_balance_passengers, 1),
            "crowding_risk_baseline": self.crowding_risk_baseline,
            "crowding_risk_modified": self.crowding_risk_modified,
            "net_capacity_change_per_hour": self.net_capacity_change_per_hour,
            "daily_vehicle_trips_diff": round(self.daily_vehicle_trips_diff, 1),
            "operational_summary": self.operational_summary,
            "confidence_disclaimer": self.confidence_disclaimer,
        }


def _classify_risk(utilization_pct: float) -> str:
    if utilization_pct > 100.0:
        return "CRITICAL"
    if utilization_pct >= 85.0:
        return "HIGH"
    if utilization_pct >= 60.0:
        return "MODERATE"
    return "LOW"


def run_service_simulation(scenario: SimulationScenario) -> SimulationResult:
    """Execute what-if simulation comparing planned frequency/capacity against forecast demand."""
    base_cap = int(round(scenario.baseline_trips_per_hour * scenario.vehicle_capacity))
    mod_cap = int(round(scenario.modified_trips_per_hour * scenario.vehicle_capacity))

    base_util = (scenario.forecast_hourly_demand / base_cap * 100.0) if base_cap > 0 else 0.0
    mod_util = (scenario.forecast_hourly_demand / mod_cap * 100.0) if mod_cap > 0 else 0.0

    base_balance = base_cap - scenario.forecast_hourly_demand
    mod_balance = mod_cap - scenario.forecast_hourly_demand

    risk_base = _classify_risk(base_util)
    risk_mod = _classify_risk(mod_util)

    net_cap = mod_cap - base_cap
    daily_trips_diff = (scenario.modified_trips_per_hour - scenario.baseline_trips_per_hour) * scenario.operating_hours

    if mod_util > 100.0:
        summary = (
            f"Scenario still indicates capacity deficit of {abs(int(round(mod_balance)))} passengers/hr "
            f"({round(mod_util, 1)}% utilization). Further headway compression or larger capacity vehicles recommended."
        )
    elif base_util > 100.0 and mod_util <= 85.0:
        summary = (
            f"Successfully resolves severe overcapacity! Utilization reduced from {round(base_util, 1)}% "
            f"to sustainable {round(mod_util, 1)}%, creating a buffer of {int(round(mod_balance))} spaces/hr."
        )
    elif net_cap > 0:
        summary = (
            f"Adds {net_cap} spaces/hour. Crowding risk lowered from {risk_base} to {risk_mod}."
        )
    elif net_cap < 0:
        summary = (
            f"Service frequency trimmed. Utilization increases to {round(mod_util, 1)}% with {risk_mod} risk."
        )
    else:
        summary = "No change in capacity or frequency."

    disclaimer = (
        "Simulation outputs are for decision support and scenario evaluation only. "
        "Real-world schedule changes require dispatcher review and driver schedule alignment."
    )

    return SimulationResult(
        route_id=scenario.route_id,
        vehicle_capacity=scenario.vehicle_capacity,
        forecast_hourly_demand=scenario.forecast_hourly_demand,
        baseline_trips_per_hour=scenario.baseline_trips_per_hour,
        modified_trips_per_hour=scenario.modified_trips_per_hour,
        baseline_capacity_per_hour=base_cap,
        modified_capacity_per_hour=mod_cap,
        baseline_utilization_pct=base_util,
        modified_utilization_pct=mod_util,
        baseline_balance_passengers=base_balance,
        modified_balance_passengers=mod_balance,
        crowding_risk_baseline=risk_base,
        crowding_risk_modified=risk_mod,
        net_capacity_change_per_hour=net_cap,
        daily_vehicle_trips_diff=daily_trips_diff,
        operational_summary=summary,
        confidence_disclaimer=disclaimer,
    )
