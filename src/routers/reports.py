"""Operational reports, automated alerts, and quality monitoring router."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from ..auth import UserRole, require_role
from ..data_quality import get_quality_monitor
from ..forecasting import load_data
from ..reporting import (
    export_daily_report_csv,
    generate_capacity_risk_report,
    generate_daily_passenger_report,
    is_safe_webhook_url,
    send_alert_webhook,
)

reports_router = APIRouter(tags=["reports"])


class WebhookPayload(BaseModel):
    webhook_url: str = Field(..., max_length=512)
    message: str = Field(..., max_length=1000)
    severity: str = Field(default="INFO", max_length=32)


@reports_router.get("/api/reports/daily", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_daily_passenger_report_endpoint(
    target_date: str | None = None,
    agency_id: str | None = None,
) -> dict[str, Any]:
    """Generate daily operational passenger summary report."""
    return generate_daily_passenger_report(target_date=target_date, agency_id=agency_id)


@reports_router.get("/api/reports/daily/csv", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_daily_report_csv_endpoint(
    target_date: str | None = None,
    agency_id: str | None = None,
) -> Response:
    """Export daily operational passenger report as CSV."""
    csv_str = export_daily_report_csv(target_date=target_date, agency_id=agency_id)
    return Response(
        content=csv_str,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=daily_report_{target_date or 'latest'}.csv"},
    )


@reports_router.get("/api/reports/capacity-risk", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_capacity_risk_report_endpoint(
    horizon_days: int = 7,
    agency_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate capacity risk across forecast horizon."""
    return generate_capacity_risk_report(horizon_days=horizon_days, agency_id=agency_id)


@reports_router.get("/api/quality/drift-status", dependencies=[Depends(require_role(UserRole.ANALYST))])
def get_drift_status_endpoint() -> dict[str, Any]:
    """Inspect data drift status and population stability index."""
    monitor = get_quality_monitor()
    data_dir = Path(os.getenv("BUS_FORECAST_DATA_DIR", "data"))
    data_path = data_dir / "daily_ridership.csv"
    if not data_path.exists():
        data_path = data_dir / "sample_daily_ridership.csv"

    df = None
    if data_path.exists():
        try:
            df = load_data(str(data_path))
        except Exception:
            pass
    rep = monitor.assess_drift(df)
    return rep.to_dict()


@reports_router.post("/api/reports/webhook", dependencies=[Depends(require_role(UserRole.ADMINISTRATOR))])
def trigger_alert_webhook(payload: WebhookPayload) -> dict[str, Any]:
    """Deliver operational alert to external webhook with SSRF protection."""
    url = payload.webhook_url.strip()
    if not is_safe_webhook_url(url):
        raise HTTPException(
            status_code=400,
            detail="Blocked unsafe webhook target URL (SSRF protection: internal, private, loopback, or non-HTTPS URLs are rejected)",
        )
    return send_alert_webhook(
        webhook_url=url,
        message=payload.message,
        severity=payload.severity,
    )
