"""Transit fleet operations, real-time dispatch, and occupancy analysis router."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import AuthenticatedPrincipal, UserRole, require_role, enforce_agency_access
from ..occupancy_engine import compute_route_capacity_profile, compute_trip_occupancy
from ..operations_engine import get_operations_engine
from ..service_planner import SimulationScenario, run_service_simulation

operations_router = APIRouter(tags=["operations"])


class SimulationRequest(BaseModel):
    scenario: str = Field(..., max_length=64)
    route_id: str | None = Field(default=None, max_length=64)
    headway_multiplier: float = Field(default=1.0, ge=0.2, le=5.0)
    additional_trips: int = Field(default=0, ge=-50, le=100)
    fare_adjustment_pct: float = Field(default=0.0, ge=-100.0, le=200.0)
    capacity_override: int | None = Field(default=None, ge=10, le=300)


@operations_router.get("/api/operations/alerts", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_operations_alerts(
    route_id: str | None = None,
    severity: str | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    """Active fleet anomalies including bunching, excessive gaps, and critical overcrowding."""
    engine = get_operations_engine()
    anomalies, _, _ = engine.evaluate_fleet(agency_id=agency_id)
    filtered = anomalies
    if route_id:
        filtered = [a for a in filtered if a.route_id == route_id]
    if severity:
        filtered = [a for a in filtered if a.severity.upper() == severity.upper()]
    return [a.to_dict() for a in filtered]


@operations_router.get("/api/operations/dispatch-recommendations", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_dispatch_recommendations(
    route_id: str | None = None,
    priority: str | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    """Actionable operational dispatch adjustments."""
    engine = get_operations_engine()
    _, recs, _ = engine.evaluate_fleet(agency_id=agency_id)
    filtered = recs
    if route_id:
        filtered = [r for r in filtered if r.route_id == route_id]
    if priority:
        filtered = [r for r in filtered if r.priority.upper() == priority.upper()]
    return [r.to_dict() for r in filtered]


@operations_router.get("/api/operations/metrics", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_operations_metrics(agency_id: str | None = None) -> dict[str, Any]:
    """High-level fleet operational telemetry KPIs."""
    engine = get_operations_engine()
    _, _, metrics = engine.evaluate_fleet(agency_id=agency_id)
    return metrics


@operations_router.get("/api/occupancy/trip/{trip_id}", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_trip_occupancy_endpoint(trip_id: str, agency_id: str = "default") -> dict[str, Any]:
    """Inspect calculated trip-level passenger occupancy profile."""
    occ = compute_trip_occupancy(trip_id=trip_id, agency_id=agency_id)
    if occ is None:
        return {"trip_id": trip_id, "found": False, "message": f"No telemetry available for trip '{trip_id}'"}
    return occ.to_dict()


@operations_router.get("/api/occupancy/route/{route_id}", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_route_capacity_profile_endpoint(route_id: str, agency_id: str = "default") -> dict[str, Any]:
    """Aggregated passenger volume and vehicle capacity load factors for a route."""
    prof = compute_route_capacity_profile(route_id=route_id, agency_id=agency_id)
    return prof.to_dict()


@operations_router.post("/api/simulator/evaluate", dependencies=[Depends(require_role(UserRole.ANALYST))])
def evaluate_service_simulation(req: SimulationRequest) -> dict[str, Any]:
    """Evaluate what-if service planning scenario impact on passenger demand."""
    scenario = SimulationScenario(
        scenario_name=req.scenario,
        target_route_id=req.route_id,
        headway_multiplier=req.headway_multiplier,
        additional_trips=req.additional_trips,
        fare_adjustment_pct=req.fare_adjustment_pct,
        capacity_override=req.capacity_override,
    )
    result = run_service_simulation(scenario)
    return result.to_dict()
