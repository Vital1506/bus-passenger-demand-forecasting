"""Transit network operations.

Read-only views of the network the workspace actually has data for: which routes
are covered, over which months, and how complete each series is. Nothing here
writes business data.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query
from pydantic import BaseModel, Field

from .. import monthly_data
from .deps import app_module, require
from .pagination import Page, PageParams, page_params

router = APIRouter(prefix="/api/network", tags=["Transit network operations"])

ROUTE_ID_MAX = 128


class NetworkRoute(BaseModel):
    route_id: str
    observed_months: int = 0
    first_month: str | None = None
    last_month: str | None = None
    boardings: int = 0
    name_changes: int = 0


class NetworkOverview(BaseModel):
    agency: str
    routes: int = 0
    observed_months: int = 0
    first_month: str | None = None
    latest_month: str | None = None
    total_boardings: int = 0
    daily_records: int = 0
    daily_routes: int = 0
    latest_observed_date: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class RouteCoverage(BaseModel):
    route_id: str
    months: list[dict[str, Any]] = Field(default_factory=list)
    name_history: list[dict[str, Any]] = Field(default_factory=list)
    gaps: int = 0


def _store() -> dict:
    return monthly_data.load_monthly_store(app_module().MONTHLY_STORE_PATH)


@router.get("/overview", response_model=NetworkOverview, dependencies=[Depends(require("gtfs:read"))])
def overview() -> NetworkOverview:
    """Agency coverage across monthly history and the daily forecasting pipeline."""

    app = app_module()
    status = app.api_safe(app.status_payload()) or {}
    store = _store()
    routes = monthly_data.list_monthly_routes(store)
    totals = store.get("totals", {}) if isinstance(store, dict) else {}
    return NetworkOverview(
        agency=str(store.get("agency") or app.security_registry().get("workspace_agency", "default")),
        routes=len(routes),
        observed_months=len(store.get("months", {}) or {}),
        first_month=totals.get("first_month"),
        latest_month=totals.get("latest_month"),
        total_boardings=int(totals.get("total_boardings", 0) or 0),
        daily_records=int(status.get("rows", 0) or 0),
        daily_routes=int(status.get("route_count", 0) or 0),
        latest_observed_date=status.get("latest_observed_date"),
        labels={
            "monthly": "historical",
            "daily": "imported" if status.get("data_loaded") else "unavailable",
        },
    )


@router.get("/routes", dependencies=[Depends(require("gtfs:read"))])
def list_routes(
    params: PageParams = Depends(page_params),
    q: str | None = Query(default=None, max_length=ROUTE_ID_MAX, description="Substring match on the route id."),
) -> Page[NetworkRoute]:
    """Paginated route coverage, alphabetical by route id."""

    routes = monthly_data.list_monthly_routes(_store())
    if q:
        needle = q.strip().lower()
        routes = [route for route in routes if needle in str(route.get("route_id", "")).lower()]
    routes.sort(key=lambda route: str(route.get("route_id", "")))
    items = [
        NetworkRoute(
            route_id=str(route.get("route_id")),
            observed_months=int(route.get("month_count") or route.get("observed_months") or 0),
            first_month=route.get("first_month"),
            last_month=route.get("last_month"),
            boardings=int(route.get("total_boardings") or route.get("boardings") or 0),
            name_changes=len(route.get("name_history", []) or []),
        )
        for route in routes
    ]
    return Page.build(items, params)


@router.get("/routes/{route_id}/coverage", response_model=RouteCoverage, dependencies=[Depends(require("gtfs:read"))])
def route_coverage(route_id: str = PathParam(min_length=1, max_length=ROUTE_ID_MAX)) -> RouteCoverage:
    """Month-by-month coverage and the recorded name history for one route."""

    history = monthly_data.get_monthly_route_history(_store(), route_id)
    if history is None:
        raise HTTPException(status_code=404, detail="Unknown route")
    months = history.get("history", history.get("months", []) or [])
    gaps = sum(1 for month in months if month.get("gap") or month.get("boardings") is None)
    return RouteCoverage(
        route_id=route_id,
        months=months,
        name_history=history.get("name_history", []) or [],
        gaps=gaps,
    )
