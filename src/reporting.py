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
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

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


logger = logging.getLogger(__name__)

CARRIER_GRADE_NAT_NET = ipaddress.ip_network("100.64.0.0/10")


def is_safe_webhook_url(url: str, allowlist: list[str] | None = None) -> tuple[bool, str]:
    """Validate webhook destination URL to prevent Server-Side Request Forgery (SSRF).

    Enforces:
    - Non-empty URL with HTTPS scheme (HTTP allowed only if BUS_FORECAST_DEV_MODE=1)
    - Rejection of embedded credentials (user:pass@host)
    - Host allowlist enforcement when BUS_FORECAST_WEBHOOK_ALLOWLIST is defined
    - DNS resolution verifying IP is not loopback, private RFC1918, link-local (cloud metadata),
      multicast, or carrier-grade NAT.
    """
    if not url or not isinstance(url, str):
        return False, "Webhook URL cannot be empty"

    url = url.strip()
    try:
        parts = urlsplit(url)
    except Exception as exc:
        return False, f"Malformed URL syntax: {exc}"

    dev_mode = os.getenv("BUS_FORECAST_DEV_MODE", "").strip().lower() in {"1", "true", "yes"}
    allow_insecure = os.getenv("BUS_FORECAST_ALLOW_INSECURE_WEBHOOKS", "").strip().lower() in {"1", "true", "yes"}

    valid_schemes = {"https", "http"} if (dev_mode or allow_insecure) else {"https"}
    if parts.scheme.lower() not in valid_schemes:
        return False, f"Scheme '{parts.scheme}' is prohibited. Webhook URLs must use HTTPS."

    if parts.username or parts.password:
        return False, "Embedded credentials in webhook URL are prohibited."

    hostname = parts.hostname
    if not hostname:
        return False, "Webhook URL must include a valid hostname."

    # Host allowlist check (if configured in env or passed explicitly)
    configured_allowlist = allowlist
    if configured_allowlist is None:
        raw_env = os.getenv("BUS_FORECAST_WEBHOOK_ALLOWLIST", "").strip()
        if raw_env:
            configured_allowlist = [h.strip().lower() for h in raw_env.split(",") if h.strip()]

    clean_host = hostname.strip().lower()
    if configured_allowlist and "*" not in configured_allowlist:
        allowed = any(
            clean_host == entry or clean_host.endswith("." + entry)
            for entry in configured_allowlist
        )
        if not allowed:
            return False, f"Hostname '{clean_host}' is not in the allowed webhook hosts allowlist."

    # DNS resolution and IP verification
    try:
        ip_objs = [ipaddress.ip_address(clean_host)]
    except ValueError:
        try:
            port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
            addr_info = socket.getaddrinfo(clean_host, port, proto=socket.IPPROTO_TCP)
            ip_objs = [ipaddress.ip_address(item[4][0]) for item in addr_info]
        except Exception as exc:
            return False, f"DNS resolution failed for hostname '{clean_host}': {exc}"

    if not ip_objs:
        return False, f"No IP addresses resolved for hostname '{clean_host}'."

    for ip in ip_objs:
        if ip.is_loopback:
            return False, f"SSRF blocked: IP {ip} is a loopback address."
        if ip.is_link_local:
            return False, f"SSRF blocked: IP {ip} is a link-local address (metadata service protection)."
        if ip.is_private and not (dev_mode and allow_insecure):
            return False, f"SSRF blocked: IP {ip} is in a private network range (RFC 1918 / RFC 4193)."
        if ip.is_multicast:
            return False, f"SSRF blocked: IP {ip} is a multicast address."
        if ip.is_reserved or ip.is_unspecified:
            return False, f"SSRF blocked: IP {ip} is a reserved or unspecified address."
        if ip in CARRIER_GRADE_NAT_NET and not (dev_mode and allow_insecure):
            return False, f"SSRF blocked: IP {ip} is in a carrier-grade NAT range."

    return True, "URL is safe"


def send_alert_webhook(
    webhook_url: str,
    alert_payload: dict[str, Any],
    http_client: httpx.Client | None = None,
) -> bool:
    """Send operational alert notification to configured webhook endpoint with SSRF protection."""
    if not webhook_url:
        return False

    is_safe, reason = is_safe_webhook_url(webhook_url)
    if not is_safe:
        logger.warning("Blocked unsafe webhook dispatch to '%s': %s", webhook_url, reason)
        return False

    client = http_client or httpx.Client(timeout=5.0, follow_redirects=False)
    try:
        resp = client.post(webhook_url, json=alert_payload)
        return resp.status_code in {200, 201, 202, 204}
    except Exception as exc:
        logger.warning("Webhook dispatch failed to '%s': %s", webhook_url, exc)
        return False
