"""Forecasting engine, accuracy ledger, and model registry router."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Response
import pandas as pd

from ..auth import AuthenticatedPrincipal, UserRole, require_role, enforce_agency_access
from ..db.repository import compute_forecast_accuracy, list_model_artifacts
from ..db.session import get_db_session
from ..hybrid_forecasting import (
    build_intraday_route_forecast,
    build_network_intraday_curve,
)

forecast_router = APIRouter(tags=["forecast"])


@forecast_router.get("/api/forecast/accuracy")
def get_forecast_accuracy(
    window_days: int = 30,
    agency_id: str | None = None,
    principal: AuthenticatedPrincipal = Depends(require_role(UserRole.ANALYST)),
) -> dict[str, Any]:
    """Compute rolling forecast accuracy (WAPE, bias) per route and lead day vs seasonal naive."""
    effective_agency = enforce_agency_access(principal, agency_id)
    with get_db_session() as session:
        return compute_forecast_accuracy(session, agency_id=effective_agency, window_days=window_days)


@forecast_router.get("/api/model/registry")
def get_model_registry(
    status: str | None = None,
    agency_id: str | None = None,
    principal: AuthenticatedPrincipal = Depends(require_role(UserRole.ANALYST)),
) -> list[dict[str, Any]]:
    """List registered candidate, active, and retired models in the Model Registry."""
    effective_agency = enforce_agency_access(principal, agency_id)
    with get_db_session() as session:
        return list_model_artifacts(session, agency_id=effective_agency, status=status)
