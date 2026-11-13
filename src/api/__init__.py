"""Domain API modules.

The application grew as one module. This package splits the backend into
separated domains that share one set of conventions:

* authentication and authorization come from :mod:`src.api.deps`, which reuses
  the existing ``authorize`` dependency, RBAC, agency scoping, rate limiting and
  audit trail;
* list endpoints return the :class:`~src.api.pagination.Page` envelope;
* write endpoints accept an ``Idempotency-Key`` header and replay their first
  result on retries;
* long-running work (training, imports, report builds) runs through
  :mod:`src.api.jobs` on worker threads instead of blocking the event loop.
"""

from __future__ import annotations

from . import (
    alerts,
    auth,
    forecasting_api,
    gtfs_realtime,
    gtfs_static_api,
    network,
    observations,
    occupancy,
    reporting_api,
    sources,
    weather,
)

API_DESCRIPTION = """
Daily bus passenger demand forecasting, split into domain modules.

**Conventions**

* Every private endpoint authenticates through the workspace credential, then
  enforces an explicit permission (`data:read`, `gtfs:write`, `model:train`, ...)
  scoped to the caller's agency. Every denied and allowed write is audited.
* List endpoints are paginated with `page` / `page_size` (max 200) and return
  `items`, `total`, `pages`, `has_next` and `has_previous`.
* Write endpoints accept an optional `Idempotency-Key` header. Retrying with the
  same key returns the original response and sets `Idempotent-Replayed: true`
  instead of writing a second record. Store writes are atomic file replacements.
* Training, GTFS imports and report builds return `202` with a job handle.
  Poll the job endpoint; workers run off the event loop.
* Aggregate data only. Passenger-level identifiers are rejected by schema.
"""

TAG_METADATA = [
    {"name": "Authentication", "description": "Capability matrix and the redacted security audit trail."},
    {"name": "Transit network operations", "description": "Agency coverage, routes, and per-route coverage."},
    {"name": "GTFS static ingestion", "description": "Validate a feed synchronously, import it as a background job."},
    {"name": "GTFS-Realtime ingestion", "description": "Deduplicated realtime snapshot ingestion and status."},
    {"name": "Passenger observation ingestion", "description": "Aggregate route/day counts; personal identifiers refused."},
    {"name": "Forecasting", "description": "Training and forecast refreshes as pollable background jobs."},
    {"name": "Weather integration", "description": "Optional coarse station observations used as demand context."},
    {"name": "Occupancy calculation", "description": "Derived utilisation from forecast and capacity assumptions."},
    {"name": "Alert generation", "description": "Detected conditions stored as aggregate alerts."},
    {"name": "Reporting", "description": "Report catalog plus background report builds and run history."},
    {"name": "Data source management", "description": "Registry of feeds with cadence and freshness expectations."},
]

ROUTERS = (
    auth.router,
    network.router,
    gtfs_static_api.router,
    gtfs_realtime.router,
    observations.router,
    forecasting_api.router,
    weather.router,
    occupancy.router,
    alerts.router,
    reporting_api.router,
    sources.router,
)

__all__ = ["API_DESCRIPTION", "ROUTERS", "TAG_METADATA"]
