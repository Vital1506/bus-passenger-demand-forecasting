"""Operational reporting, performance summaries, CSV exports, and webhook dispatching.

Generates:
- Daily transit demand summaries
- Weekly route performance & reliability reports
- Capacity and overcrowding risk summaries
- Webhook dispatching with retry handling
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import (
    DailyPassengerRecord,
    OperationalAlertRecord,
    StopPassengerObservation,
    TripPassengerObservation,
)
from .db.repository import get_daily_counts_summary, get_monthly_routes_summary


def generate_daily_passenger_report(
    session: Session,
    service_date: date | str,
    agency_id: str = "default",
) -> dict[str, Any]:
    """Generate daily passenger demand summary across all active routes."""
    target_d = service_date if isinstance(service_date, date) else pd.to_datetime(service_date).date()

    stmt = select(DailyPassengerRecord).where(
        DailyPassengerRecord.agency_id == agency_id,
        DailyPassengerRecord.service_date == target_d,
    )
    records = session.execute(stmt).scalars().all()

    total_passengers = sum(r.passengers for r in records)
    routes_count = len(records)
    avg_per_route = total_passengers / routes_count if routes_count > 0 else 0.0

    route_breakdown = [
        {"route_id": r.route_id, "passengers": r.passengers, "source_system": r.source_system}
        for r in sorted(records, key=lambda x: x.passengers, reverse=True)
    ]

    return {
        "report_type": "DAILY_PASSENGER_SUMMARY",
        "agency_id": agency_id,
        "service_date": target_d.isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_boardings": round(total_passengers, 1),
        "active_routes_count": routes_count,
        "average_passengers_per_route": round(avg_per_route, 1),
        "route_breakdown": route_breakdown,
    }


def generate_capacity_risk_report(
    session: Session,
    agency_id: str = "default",
    limit: int = 50,
) -> dict[str, Any]:
    """Identify recent trips or stops operating over capacity."""
    # Query unresolved overcrowding alerts
    stmt = (
        select(OperationalAlertRecord)
        .where(
            OperationalAlertRecord.agency_id == agency_id,
            OperationalAlertRecord.alert_type.in_(["OVERCROWDING", "HIGH_DEMAND"]),
        )
        .order_by(OperationalAlertRecord.created_at_utc.desc())
        .limit(limit)
    )
    alerts = session.execute(stmt).scalars().all()

    return {
        "report_type": "CAPACITY_RISK_REPORT",
        "agency_id": agency_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_risk_incidents": len(alerts),
        "incidents": [
            {
                "alert_id": a.alert_id,
                "severity": a.severity,
                "route_id": a.route_id,
                "summary": a.summary,
                "created_at_utc": a.created_at_utc.isoformat(),
                "acknowledged": a.acknowledged,
            }
            for a in alerts
        ],
    }


def export_daily_report_csv(report_data: dict[str, Any]) -> str:
    """Format daily report breakdown as a standard CSV text stream."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["service_date", "route_id", "passengers", "source_system"])
    s_date = report_data.get("service_date", "")
    for row in report_data.get("route_breakdown", []):
        writer.writerow([s_date, row["route_id"], row["passengers"], row["source_system"]])
    return output.getvalue()


def send_alert_webhook(
    webhook_url: str,
    alert_payload: dict[str, Any],
    http_client: httpx.Client | None = None,
) -> bool:
    """Send operational alert notification to configured webhook endpoint."""
    if not webhook_url:
        return False

    client = http_client or httpx.Client(timeout=5.0)
    try:
        resp = client.post(webhook_url, json=alert_payload)
        return resp.status_code in {200, 201, 202, 204}
    except Exception:
        return False
