"""External GTFS-Realtime provider feed registry, polling coordinator, and connection monitor.

Security & Integrity:
- Server-side credentials resolution from environment variables (never exposed in client responses)
- Automatic feed freshness tracking (flags STALE if feed age > max_stale_seconds)
- Graceful error recovery: feed outages, network timeouts, invalid protobuf payloads
- Clear status determination: CONNECTED, STALE, DISCONNECTED, ERROR
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .models import RealtimeFeedMessage
from .protobuf_parser import parse_realtime_feed
from .service import get_realtime_service


@dataclass
class FeedProviderConfig:
    agency_id: str
    agency_name: str
    vehicle_positions_url: str | None = None
    trip_updates_url: str | None = None
    alerts_url: str | None = None
    auth_header_name: str | None = None
    auth_token_env_var: str | None = None
    polling_interval_seconds: int = 30
    max_stale_seconds: int = 180
    is_enabled: bool = True
    last_poll_attempt_utc: float | None = None
    last_successful_poll_utc: float | None = None
    last_error_message: str | None = None
    total_polls: int = 0
    failed_polls: int = 0

    @property
    def has_endpoints(self) -> bool:
        return bool(self.vehicle_positions_url or self.trip_updates_url or self.alerts_url)

    def get_auth_headers(self) -> dict[str, str]:
        """Resolve authorization header from environment without leaking token into config."""
        if not self.auth_header_name or not self.auth_token_env_var:
            return {}
        token = os.getenv(self.auth_token_env_var, "").strip()
        if not token:
            return {}
        return {self.auth_header_name: token}

    def connection_status(self) -> str:
        """Compute connection status: CONNECTED, STALE, DISCONNECTED, ERROR."""
        if not self.is_enabled or not self.has_endpoints:
            return "DISCONNECTED"

        now = time.time()
        if self.last_successful_poll_utc is None:
            if self.last_error_message:
                return "ERROR"
            return "DISCONNECTED"

        age = now - self.last_successful_poll_utc
        if age > self.max_stale_seconds:
            return "STALE"
        if self.last_error_message and age > self.polling_interval_seconds * 2:
            return "ERROR"
        return "CONNECTED"

    def to_status_dict(self) -> dict[str, Any]:
        """Sanitized representation suitable for public API exposure."""
        status = self.connection_status()
        now = time.time()
        freshness_seconds = round(now - self.last_successful_poll_utc, 1) if self.last_successful_poll_utc else None

        return {
            "agency_id": self.agency_id,
            "agency_name": self.agency_name,
            "status": status,
            "is_configured": self.has_endpoints,
            "is_enabled": self.is_enabled,
            "last_successful_poll_utc": (
                datetime.fromtimestamp(self.last_successful_poll_utc, timezone.utc).isoformat()
                if self.last_successful_poll_utc else None
            ),
            "freshness_seconds": freshness_seconds,
            "max_stale_threshold_seconds": self.max_stale_seconds,
            "last_error": self.last_error_message,
            "endpoints": {
                "vehicle_positions": bool(self.vehicle_positions_url),
                "trip_updates": bool(self.trip_updates_url),
                "alerts": bool(self.alerts_url),
            },
            "metrics": {
                "total_polls": self.total_polls,
                "failed_polls": self.failed_polls,
            }
        }


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, FeedProviderConfig] = {}
        self._init_defaults()

    def _init_defaults(self):
        """Register default agency feed descriptors."""
        cta_config = FeedProviderConfig(
            agency_id="CTA",
            agency_name="Chicago Transit Authority",
            vehicle_positions_url=os.getenv("GTFS_RT_VEHICLES_URL"),
            trip_updates_url=os.getenv("GTFS_RT_TRIPS_URL"),
            alerts_url=os.getenv("GTFS_RT_ALERTS_URL"),
            auth_header_name=os.getenv("GTFS_RT_AUTH_HEADER", "x-api-key"),
            auth_token_env_var="GTFS_RT_API_KEY",
            polling_interval_seconds=int(os.getenv("GTFS_RT_POLL_INTERVAL", "30")),
            max_stale_seconds=int(os.getenv("GTFS_RT_MAX_STALE", "180")),
            is_enabled=True,
        )
        self._providers["CTA"] = cta_config

    def register_provider(self, config: FeedProviderConfig) -> None:
        self._providers[config.agency_id] = config

    def get_provider(self, agency_id: str) -> FeedProviderConfig | None:
        return self._providers.get(agency_id)

    def list_providers(self) -> list[dict[str, Any]]:
        return [p.to_status_dict() for p in self._providers.values()]

    def is_live_active(self, agency_id: str) -> bool:
        provider = self.get_provider(agency_id)
        if not provider:
            return False
        return provider.connection_status() == "CONNECTED"

    def poll_provider(self, agency_id: str, http_client: httpx.Client | None = None) -> dict[str, Any]:
        """Fetch active feed endpoints for a provider and ingest into the telemetry store."""
        provider = self.get_provider(agency_id)
        if not provider or not provider.has_endpoints:
            return {
                "success": False,
                "status": "DISCONNECTED",
                "message": f"No GTFS-RT feed URLs configured for agency '{agency_id}'",
                "ingested": 0,
            }

        provider.last_poll_attempt_utc = time.time()
        provider.total_polls += 1
        headers = provider.get_auth_headers()
        client = http_client or httpx.Client(timeout=10.0)

        ingested_counts = {"vehicles": 0, "trip_updates": 0, "alerts": 0}
        service = get_realtime_service()

        try:
            # 1. Fetch Vehicle Positions
            if provider.vehicle_positions_url:
                resp = client.get(provider.vehicle_positions_url, headers=headers)
                resp.raise_for_status()
                res = service.ingest_payload(resp.content, agency_id=agency_id)
                ingested_counts["vehicles"] += res.get("vehicles_ingested", 0)

            # 2. Fetch Trip Updates
            if provider.trip_updates_url:
                resp = client.get(provider.trip_updates_url, headers=headers)
                resp.raise_for_status()
                res = service.ingest_payload(resp.content, agency_id=agency_id)
                ingested_counts["trip_updates"] += res.get("updates_ingested", 0)

            # 3. Fetch Service Alerts
            if provider.alerts_url:
                resp = client.get(provider.alerts_url, headers=headers)
                resp.raise_for_status()
                res = service.ingest_payload(resp.content, agency_id=agency_id)
                ingested_counts["alerts"] += res.get("alerts_ingested", 0)

            provider.last_successful_poll_utc = time.time()
            provider.last_error_message = None
            return {
                "success": True,
                "status": provider.connection_status(),
                "ingested": ingested_counts,
                "freshness_seconds": 0.0,
            }

        except Exception as exc:
            provider.failed_polls += 1
            provider.last_error_message = str(exc)
            return {
                "success": False,
                "status": provider.connection_status(),
                "error": str(exc),
                "ingested": ingested_counts,
            }


_GLOBAL_PROVIDER_REGISTRY: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    global _GLOBAL_PROVIDER_REGISTRY
    if _GLOBAL_PROVIDER_REGISTRY is None:
        _GLOBAL_PROVIDER_REGISTRY = ProviderRegistry()
    return _GLOBAL_PROVIDER_REGISTRY
