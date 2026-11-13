"""Occupancy calculation.

Occupancy is derived, never observed: predicted boardings divided by the daily
capacity implied by the stored per-route assumptions. If either input is missing
the projection reports ``unavailable`` instead of inventing a number.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .deps import app_module, require
from .pagination import Page, PageParams, page_params
from .schemas import DataLabel

router = APIRouter(prefix="/api/occupancy", tags=["Occupancy calculation"])

PEAK_HOURS = 4.0
OFF_PEAK_HOURS = 12.0


class RouteOccupancy(BaseModel):
    route_id: str
    passengers: float
    daily_capacity: float
    utilisation: float
    label: DataLabel
    status: str


class OccupancyProjection(BaseModel):
    available: bool
    label: DataLabel
    reason: str | None = None
    service_days: int = 0
    routes: list[RouteOccupancy] = Field(default_factory=list)
    assumptions_configured: int = 0
    formula: str = (
        "daily_capacity = seats_per_vehicle * (peak_hours / peak_headway + off_peak_hours / off_peak_headway) * 60 / 60"
    )


def _daily_capacity(assumption: dict[str, Any]) -> float | None:
    seats = assumption.get("seats_per_vehicle")
    peak = assumption.get("peak_headway_minutes")
    off_peak = assumption.get("off_peak_headway_minutes", peak)
    if not seats or not peak:
        return None
    peak = float(peak)
    off_peak = float(off_peak or peak)
    if peak <= 0 or off_peak <= 0:
        return None
    trips = (PEAK_HOURS * 60.0) / peak + (OFF_PEAK_HOURS * 60.0) / off_peak
    return float(seats) * trips


@router.get("/projection", response_model=OccupancyProjection, dependencies=[Depends(require("reports:read"))])
def projection(days: int = Query(default=7, ge=1, le=90)) -> OccupancyProjection:
    """Projected utilisation per route for the next ``days`` of the forecast."""

    app = app_module()
    try:
        assumptions = app.read_capacity_assumptions() or {}
    except Exception:  # noqa: BLE001 - unreadable assumptions mean "unavailable"
        assumptions = {}
    forecast_path = app.FORECAST_PATH
    if not forecast_path.exists():
        return OccupancyProjection(
            available=False,
            label=DataLabel.unavailable,
            reason="No forecast has been generated yet.",
        )
    frame = pd.read_csv(forecast_path)
    if frame.empty or "route_id" not in frame or "predicted_passengers" not in frame:
        return OccupancyProjection(
            available=False,
            label=DataLabel.unavailable,
            reason="The current forecast has no route-level rows.",
        )
    frame = frame.copy()
    frame["service_date"] = pd.to_datetime(frame["service_date"], errors="coerce")
    frame = frame.dropna(subset=["service_date"]).sort_values("service_date")
    horizon = sorted(frame["service_date"].dt.date.unique())[:days]
    frame = frame.loc[frame["service_date"].dt.date.isin(horizon)]

    routes: list[RouteOccupancy] = []
    for route_id, group in frame.groupby("route_id", dropna=True):
        capacity = _daily_capacity(assumptions.get(str(route_id), {}) or {})
        passengers = float(pd.to_numeric(group["predicted_passengers"], errors="coerce").fillna(0).mean())
        if not capacity:
            routes.append(
                RouteOccupancy(
                    route_id=str(route_id),
                    passengers=round(passengers, 1),
                    daily_capacity=0.0,
                    utilisation=0.0,
                    label=DataLabel.unavailable,
                    status="no_capacity_assumption",
                )
            )
            continue
        utilisation = passengers / capacity
        label = DataLabel.stale if utilisation > 1.0 else DataLabel.forecast
        routes.append(
            RouteOccupancy(
                route_id=str(route_id),
                passengers=round(passengers, 1),
                daily_capacity=round(capacity, 1),
                utilisation=round(utilisation, 4),
                label=label,
                status="over_capacity" if utilisation > 1.0 else "within_capacity",
            )
        )
    routes.sort(key=lambda item: item.utilisation, reverse=True)
    configured = sum(1 for route in routes if route.daily_capacity > 0)
    return OccupancyProjection(
        available=bool(routes),
        label=DataLabel.forecast if configured else DataLabel.unavailable,
        reason=None if configured else "No capacity assumptions are configured for the forecasted routes.",
        service_days=len(horizon),
        routes=routes,
        assumptions_configured=configured,
    )


@router.get("/routes", dependencies=[Depends(require("reports:read"))])
def list_route_occupancy(
    params: PageParams = Depends(page_params),
    days: int = Query(default=7, ge=1, le=90),
) -> Page[RouteOccupancy]:
    """Paginated occupancy rows, highest utilisation first."""

    return Page.build(projection(days=days).routes, params)


@router.get("/assumptions", dependencies=[Depends(require("reports:read"))])
def assumptions() -> dict[str, Any]:
    """Stored per-route capacity assumptions and their derived daily capacity."""

    app = app_module()
    stored = app.read_capacity_assumptions() or {}
    rows = []
    for route_id, values in sorted(stored.items()):
        capacity = _daily_capacity(values or {})
        rows.append(
            {
                "route_id": route_id,
                "assumption": values,
                "daily_capacity": None if capacity is None else round(capacity, 1),
                "usable": bool(capacity and math.isfinite(capacity)),
            }
        )
    return {"routes": rows, "count": len(rows)}
