"""Local and hosted API for the Bus Passenger Demand Forecast app."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .forecast import build_forecast
from .forecasting import REQUIRED_COLUMNS, complete_daily_series, load_data
from . import decisions, monitoring, notifications, reports, security
from .api import API_DESCRIPTION, ROUTERS as API_ROUTERS, TAG_METADATA as API_TAG_METADATA
from .monthly_data import (
    DATASET_NAME as MONTHLY_DATASET_NAME,
    MonthlyDataError,
    build_monthly_status,
    get_monthly_route_history,
    ingest_monthly_csv,
    list_monthly_routes,
    load_monthly_store,
    monthly_audit_path,
    monthly_store_path,
    read_monthly_audit,
)
from .route_identity import canonicalize_manual_route_id
from .schedule_import import schedules_to_synthetic_counts
from .train import train_project


def project_path(relative: str) -> Path:
    """Resolve assets both from source and from a PyInstaller bundle."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parent.parent
    return root / relative


def user_data_dir() -> Path:
    configured = os.getenv("BUS_FORECAST_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        return Path(os.getenv("LOCALAPPDATA", Path.home())) / "BusDemandForecast"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BusDemandForecast"
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "bus-demand-forecast"


DATA_DIR = user_data_dir()
ARTIFACT_DIR = DATA_DIR / "artifacts"
DATA_PATH = DATA_DIR / "daily_ridership.csv"
MODEL_PATH = ARTIFACT_DIR / "model.joblib"
METRICS_PATH = ARTIFACT_DIR / "backtest_metrics.json"
PREDICTIONS_PATH = ARTIFACT_DIR / "backtest_predictions.csv"
HOLDOUT_PREDICTIONS_PATH = ARTIFACT_DIR / "holdout_predictions.csv"
FORECAST_PATH = DATA_DIR / "forecast.csv"
META_PATH = DATA_DIR / "data_source.json"
AUDIT_PATH = DATA_DIR / "ingest_audit.jsonl"
MONITORING_POLICY_PATH = DATA_DIR / "monitoring_policy.json"
NOTIFICATION_POLICY_PATH = DATA_DIR / "notification_policy.json"
NOTIFICATION_STORE_PATH = DATA_DIR / "notifications.jsonl"
CAPACITY_ASSUMPTIONS_PATH = DATA_DIR / "capacity_assumptions.json"
SERVICE_ALERTS_PATH = DATA_DIR / "service_alerts.json"
SECURITY_REGISTRY_PATH = DATA_DIR / "security.json"
SECURITY_AUDIT_PATH = DATA_DIR / "security_audit.jsonl"
SECURITY_LOCK = threading.Lock()
MONTHLY_STORE_PATH = monthly_store_path(DATA_DIR)
MONTHLY_AUDIT_PATH = monthly_audit_path(DATA_DIR)
PUBLIC_MONTHLY_READ_ENV = "BUS_FORECAST_PUBLIC_MONTHLY_READ"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PIPELINE_LOCK = threading.Lock()
MONTHLY_LOCK = threading.Lock()
GTFS_LOCK = threading.Lock()
PUBLIC_DEMO_LOCK = threading.Lock()
MONITORING_LOCK = threading.Lock()
NOTIFICATION_LOCK = threading.Lock()
PUBLIC_DEMO_CACHE: dict | None = None
PUBLIC_DEMO_ARTIFACTS: dict[str, Path] = {}

WEB_DIR = project_path("web")
WEB_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Bus Passenger Demand Forecast",
    description=API_DESCRIPTION,
    version="0.2.0",
    openapi_tags=API_TAG_METADATA,
)
try:
    CORS_SETTINGS = security.cors_settings(os.getenv("BUS_FORECAST_CORS_ORIGINS", ""))
except security.InputValidationError as exc:
    sys.stderr.write(f"Ignoring invalid BUS_FORECAST_CORS_ORIGINS: {exc.detail}\n")
    CORS_SETTINGS = security.cors_settings("")
if CORS_SETTINGS["allow_origins"]:
    app.add_middleware(CORSMiddleware, **CORS_SETTINGS)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
for _router in API_ROUTERS:
    app.include_router(_router)


def auth_required() -> bool:
    return os.getenv("BUS_FORECAST_REQUIRE_AUTH", "").lower() in {"1", "true", "yes"}


def rate_limit_per_minute() -> int:
    raw = os.getenv("BUS_FORECAST_RATE_LIMIT_PER_MINUTE", "240")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 240


RATE_LIMITER = security.RateLimiter(limit=rate_limit_per_minute(), window_seconds=60.0)

# One permission per private endpoint. Everything not listed needs data:read.
SECURITY_POLICY: tuple[tuple[str, str, str], ...] = (
    # Domain API modules (src/api) — specific rules first.
    ("GET", r"^/api/auth/capabilities$", "data:read"),
    ("GET", r"^/api/auth", "security:admin"),
    ("POST", r"^/api/alerts", "notifications:write"),
    ("GET", r"^/api/alerts", "notifications:read"),
    ("POST", r"^/api/reporting/runs$", "reports:read"),
    ("GET", r"^/api/reporting", "reports:read"),
    ("POST", r"^/api/forecasting", "model:train"),
    ("GET", r"^/api/forecasting", "forecast:run"),
    ("POST", r"^/api/ingestion/observations$", "data:write"),
    ("GET", r"^/api/ingestion/observations", "data:read"),
    ("POST", r"^/api/ingestion/gtfs-static", "gtfs:write"),
    ("GET", r"^/api/ingestion/gtfs-static", "gtfs:read"),
    ("POST", r"^/api/ingestion/gtfs-realtime", "gtfs:write"),
    ("DELETE", r"^/api/ingestion/gtfs-realtime", "security:admin"),
    ("GET", r"^/api/ingestion/gtfs-realtime", "gtfs:read"),
    ("POST", r"^/api/weather/observations$", "data:write"),
    ("DELETE", r"^/api/weather", "security:admin"),
    ("GET", r"^/api/weather", "data:read"),
    ("GET", r"^/api/occupancy", "reports:read"),
    ("POST", r"^/api/sources", "gtfs:write"),
    ("PATCH", r"^/api/sources", "gtfs:write"),
    ("DELETE", r"^/api/sources", "gtfs:write"),
    ("GET", r"^/api/sources", "gtfs:read"),
    ("GET", r"^/api/network", "gtfs:read"),
    ("POST", r"^/api/data/upload$", "data:write"),
    ("POST", r"^/api/data/ingest$", "data:write"),
    ("POST", r"^/api/data/demo$", "data:write"),
    ("POST", r"^/api/train$", "model:train"),
    ("POST", r"^/api/forecast$", "forecast:run"),
    ("POST", r"^/api/monthly-data/upload$", "monthly:write"),
    ("POST", r"^/api/gtfs/upload$", "gtfs:write"),
    ("PUT", r"^/api/monitoring/policy$", "settings:write"),
    ("PUT", r"^/api/notifications/policy$", "notifications:write"),
    ("POST", r"^/api/notifications/evaluate$", "notifications:write"),
    ("POST", r"^/api/notifications/[^/]+/acknowledge$", "notifications:write"),
    ("PUT", r"^/api/reports/capacity$", "settings:write"),
    ("GET", r"^/api/security/me$", "data:read"),
    ("GET", r"^/api/security", "security:admin"),
    ("POST", r"^/api/security", "security:admin"),
    ("GET", r"^/api/migrations$", "security:admin"),
    ("GET", r"^/api/storage/health$", "security:admin"),
    ("GET", r"^/api/monitoring", "monitoring:read"),
    ("GET", r"^/api/notifications", "notifications:read"),
    ("GET", r"^/api/reports", "reports:read"),
    ("GET", r"^/api/monthly-data", "monthly:read"),
    ("GET", r"^/api/gtfs", "gtfs:read"),
)


def required_permission(method: str, path: str) -> str:
    for rule_method, pattern, permission in SECURITY_POLICY:
        if rule_method == method and re.match(pattern, path):
            return permission
    return "data:read"


def security_registry() -> dict:
    try:
        return security.load_registry(
            SECURITY_REGISTRY_PATH, default_agency=os.getenv("BUS_FORECAST_AGENCY", "default")
        )
    except security.RegistryError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc


def presented_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def legacy_principal(workspace: str) -> security.Principal:
    return security.Principal(
        id="legacy-shared-key",
        name="Shared operator key",
        role=security.ROLE_ADMIN,
        agency=workspace,
        active=True,
        legacy=True,
        permissions=security.ROLE_PERMISSIONS[security.ROLE_ADMIN],
    )


def resolve_principal(authorization: str | None, registry: dict) -> security.Principal:
    workspace = registry["workspace_agency"]
    if not auth_required():
        return security.local_principal(workspace)
    key = presented_bearer(authorization)
    legacy_key = os.getenv("BUS_FORECAST_API_KEY")
    if not legacy_key and not registry.get("principals"):
        raise security.AuthenticationError("Hosted API authentication is not configured")
    if security.key_matches_legacy(key, legacy_key):
        return legacy_principal(workspace)
    principal = security.find_principal_by_key(registry, key)
    if principal is None:
        raise security.AuthenticationError("Enter the API access key in app settings")
    return principal


def audit_security(
    *,
    action: str,
    outcome: str,
    request: Request | None = None,
    principal: security.Principal | None = None,
    target: str | None = None,
    detail: Any = None,
    status_code: int | None = None,
) -> None:
    security.append_audit(
        SECURITY_AUDIT_PATH,
        security.audit_event(
            action=action,
            outcome=outcome,
            principal=principal,
            target=target,
            detail=detail,
            method=request.method if request else None,
            path=request.url.path if request else None,
            client=request.client.host if request and request.client else None,
            status_code=status_code,
        ),
    )


def authorize(request: Request, authorization: str | None = Header(default=None)) -> security.Principal:
    """Authenticate, rate limit, authorize, and audit every private request."""
    registry = security_registry()
    workspace = registry["workspace_agency"]
    try:
        principal = resolve_principal(authorization, registry)
    except security.SecurityError as exc:
        audit_security(action="authenticate", outcome="denied", request=request, detail=exc.detail, status_code=exc.status_code)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    rate_key = principal.id if auth_required() else (request.client.host if request.client else "local")
    retry_after = RATE_LIMITER.allow(rate_key)
    if retry_after > 0:
        audit_security(
            action="rate_limit",
            outcome="denied",
            request=request,
            principal=principal,
            detail=f"Retry after {retry_after:.1f}s",
            status_code=429,
        )
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Retry shortly.",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    permission = required_permission(request.method, request.url.path)
    try:
        security.require(principal, permission, workspace_agency=workspace)
    except security.SecurityError as exc:
        audit_security(
            action=permission,
            outcome="denied",
            request=request,
            principal=principal,
            detail=exc.detail,
            status_code=exc.status_code,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        audit_security(action=permission, outcome="allowed", request=request, principal=principal, status_code=200)
        if auth_required() and not principal.legacy:
            with SECURITY_LOCK:
                security.touch_principal(registry, principal.id)
                try:
                    security.save_registry(SECURITY_REGISTRY_PATH, registry)
                except OSError:
                    pass
    return principal


def protect_pipeline():
    """Serialize uploads, training runs, and forecast writes."""
    if not PIPELINE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A data update or forecasting run is already in progress")
    try:
        yield
    finally:
        PIPELINE_LOCK.release()


def protect_monthly_store():
    """Serialize monthly uploads separately from the daily pipeline lock."""
    if not MONTHLY_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A monthly data upload is already in progress")
    try:
        yield
    finally:
        MONTHLY_LOCK.release()


def public_monthly_read_enabled() -> bool:
    return os.getenv(PUBLIC_MONTHLY_READ_ENV, "").strip().lower() in {"1", "true", "yes"}


def require_public_monthly_read() -> None:
    """Hide the public monthly endpoints entirely unless explicitly enabled."""
    if not public_monthly_read_enabled():
        raise HTTPException(status_code=404, detail="Not found")


class TrainRequest(BaseModel):
    horizon: int = Field(default=7, ge=1, le=90)
    folds: int = Field(default=4, ge=1, le=8)
    missing_days: Literal["error", "zero"] = "error"


class ForecastRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=90)
    missing_days: Literal["error", "zero"] = "error"


class DailyCountRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    route_id: str = Field(min_length=1, max_length=128)
    passengers: float = Field(ge=0)


class DataIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_system: str = Field(default="company_feed", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_. -]+$")
    records: list[DailyCountRecord] = Field(min_length=1, max_length=5000)
    missing_days: Literal["error", "zero"] = "error"


def read_metadata() -> dict:
    if not META_PATH.exists():
        return {"source": "none"}
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"source": "unknown"}


def write_metadata(
    source: str,
    source_system: str | None = None,
    record_count: int | None = None,
    route_count: int | None = None,
) -> None:
    metadata = {"source": source, "updated_at_utc": pd.Timestamp.now(tz="UTC").isoformat()}
    if source_system:
        metadata["source_system"] = source_system
    if record_count is not None:
        metadata["record_count"] = int(record_count)
    if route_count is not None:
        metadata["route_count"] = int(route_count)
    temporary = META_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    temporary.replace(META_PATH)


def record_audit_event(action: str, source_system: str, record_count: int, route_count: int) -> None:
    """Append an aggregate-only data operation event; never record passenger rows."""
    event = {
        "timestamp_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "action": action,
        "source_system": source_system,
        "record_count": int(record_count),
        "route_count": int(route_count),
    }
    with AUDIT_PATH.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(event, separators=(",", ":")) + "\n")


def read_recent_activity(limit: int = 12) -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
        events = []
        for line in lines:
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except json.JSONDecodeError:
                continue
        return list(reversed(events))
    except OSError:
        return []


def persist_daily_data(data: pd.DataFrame) -> None:
    output = data[["service_date", "route_id", "passengers"]].copy()
    output["service_date"] = pd.to_datetime(output["service_date"]).dt.strftime("%Y-%m-%d")
    temporary = DATA_PATH.with_suffix(".uploading")
    output.to_csv(temporary, index=False)
    temporary.replace(DATA_PATH)


def api_safe(value):
    """Replace non-finite metric values with JSON null."""
    if isinstance(value, dict):
        return {key: api_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [api_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def status_payload() -> dict:
    metadata = read_metadata()
    result = {
        "data_loaded": DATA_PATH.exists(),
        "source": metadata.get("source", "none"),
        "source_system": metadata.get("source_system"),
        "data_updated_at": metadata.get("updated_at_utc"),
        "recent_activity": read_recent_activity(),
        "rows": 0,
        "route_count": 0,
        "latest_observed_date": None,
        "model_trained": MODEL_PATH.exists(),
        "trained_through": None,
        "horizon_days": None,
        "metrics": None,
        "forecasts": [],
        "forecast_updated_at": None,
    }
    if DATA_PATH.exists():
        try:
            data = load_data(str(DATA_PATH))
            result.update({
                "rows": int(len(data)),
                "route_count": int(data["route_id"].nunique()),
                "latest_observed_date": data["service_date"].max().date().isoformat(),
            })
        except (OSError, ValueError):
            result["data_loaded"] = False
    if MODEL_PATH.exists():
        try:
            import joblib

            bundle = joblib.load(MODEL_PATH)
            result["trained_through"] = bundle.get("trained_through")
            result["horizon_days"] = int(bundle.get("horizon", 0))
        except (OSError, ValueError, KeyError):
            result["model_trained"] = False
    if METRICS_PATH.exists():
        try:
            result["metrics"] = api_safe(json.loads(METRICS_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    if FORECAST_PATH.exists():
        try:
            forecasts = pd.read_csv(FORECAST_PATH)
            result["forecasts"] = forecasts.to_dict(orient="records")
            result["forecast_updated_at"] = pd.Timestamp(FORECAST_PATH.stat().st_mtime, unit="s", tz="UTC").isoformat()
        except (OSError, pd.errors.ParserError):
            pass
    return result


def save_forecast(days: int, missing_days: str = "error") -> pd.DataFrame:
    if not DATA_PATH.exists():
        raise HTTPException(status_code=409, detail="Upload daily route counts or load demo data first")
    if not MODEL_PATH.exists():
        raise HTTPException(status_code=409, detail="Train a model before generating a forecast")
    try:
        forecast = build_forecast(DATA_PATH, MODEL_PATH, days, missing_days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    temporary = FORECAST_PATH.with_suffix(".tmp")
    forecast.to_csv(temporary, index=False)
    temporary.replace(FORECAST_PATH)
    return forecast


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": app.version}


def read_daily_data():
    """Latest validated daily observations, or None when unavailable."""
    if not DATA_PATH.exists():
        return None
    try:
        return load_data(str(DATA_PATH))
    except (OSError, ValueError):
        return None


def read_forecast_frame():
    """Latest forecast rows, or None when unavailable."""
    if not FORECAST_PATH.exists():
        return None
    try:
        return pd.read_csv(FORECAST_PATH)
    except (OSError, pd.errors.ParserError):
        return None


def read_metrics_file() -> dict | None:
    """Backtest metrics document, or None when unavailable."""
    if not METRICS_PATH.exists():
        return None
    try:
        payload = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_capacity_assumptions() -> dict:
    """Operator-covered capacity assumptions used by reports and alerts."""
    if not CAPACITY_ASSUMPTIONS_PATH.exists():
        return {}
    try:
        return reports.normalize_capacity_assumptions(
            json.loads(CAPACITY_ASSUMPTIONS_PATH.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def read_disruptions() -> list[dict] | None:
    """Active GTFS-Realtime service alerts: database first, then an exported JSON list."""
    try:
        from sqlalchemy import select

        from .db import session_scope
        from .schema import GtfsRealtimeServiceAlert

        with session_scope() as session:
            rows = session.execute(
                select(GtfsRealtimeServiceAlert)
                .where(GtfsRealtimeServiceAlert.active.is_(True))
                .order_by(GtfsRealtimeServiceAlert.feed_snapshot_at.desc())
                .limit(200)
            ).scalars().all()
        items = [
            {
                "start": row.feed_snapshot_at.isoformat() if row.feed_snapshot_at else None,
                "agency": row.agency_code,
                "route": "all",
                "effect": "SERVICE_ALERT",
                "cause": "UNKNOWN",
                "header": row.header_text or row.description_text or "Service alert",
            }
            for row in rows
        ]
        if items:
            return items
    except Exception:  # the database layer is optional for this prototype
        pass
    if SERVICE_ALERTS_PATH.exists():
        try:
            payload = json.loads(SERVICE_ALERTS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, list) else None
    return None


def reports_inputs() -> dict:
    """Everything the report builders read, resolved once per request."""
    return {
        "data": read_daily_data(),
        "forecast": read_forecast_frame(),
        "metrics": read_metrics_file(),
        "disruptions": read_disruptions(),
        "capacity": read_capacity_assumptions(),
    }


def monitoring_payload() -> dict:
    """Read the deployed artifacts and build the model-monitoring snapshot."""
    data = read_daily_data()
    bundle = None
    if MODEL_PATH.exists():
        try:
            import joblib

            bundle = joblib.load(MODEL_PATH)
        except (OSError, ValueError, KeyError):
            bundle = None
    metrics = read_metrics_file()
    forecast = read_forecast_frame()
    forecast_updated_at = None
    if forecast is not None:
        try:
            forecast_updated_at = pd.Timestamp(FORECAST_PATH.stat().st_mtime, unit="s", tz="UTC").isoformat()
        except OSError:
            forecast_updated_at = None
    snapshot = monitoring.build_monitoring_snapshot(
        data=data,
        model_bundle=bundle if isinstance(bundle, dict) else None,
        metrics=metrics,
        forecast=forecast,
        forecast_updated_at=forecast_updated_at,
        policy=monitoring.load_policy(MONITORING_POLICY_PATH),
    )
    snapshot["source"] = read_metadata().get("source", "none")
    return api_safe(snapshot)


class RetrainingPolicyPayload(BaseModel):
    """Every field is optional; omitted fields keep their current policy value."""

    max_model_age_days: float | None = Field(default=None, ge=1, le=365)
    max_data_age_days: float | None = Field(default=None, ge=0, le=90)
    max_forecast_age_days: float | None = Field(default=None, ge=0, le=90)
    max_missing_percent: float | None = Field(default=None, ge=0, le=100)
    max_wape_gap: float | None = Field(default=None, ge=0, le=1)
    max_holdout_degradation: float | None = Field(default=None, ge=0, le=10)
    distribution_shift_percent: float | None = Field(default=None, ge=1, le=500)
    distribution_window_days: float | None = Field(default=None, ge=2, le=90)
    min_observations_per_route: float | None = Field(default=None, ge=7, le=3650)


@app.get("/api/status", dependencies=[Depends(authorize)])
def get_status() -> dict:
    return status_payload()


@app.get("/api/monitoring", dependencies=[Depends(authorize)])
def get_monitoring() -> dict:
    """Model monitoring, data quality, and the current retraining recommendation."""
    return monitoring_payload()


@app.get("/api/monitoring/policy", dependencies=[Depends(authorize)])
def get_monitoring_policy() -> dict:
    return {"policy": monitoring.load_policy(MONITORING_POLICY_PATH)}


@app.put("/api/monitoring/policy", dependencies=[Depends(authorize)])
def update_monitoring_policy(payload: RetrainingPolicyPayload) -> dict:
    """Save retraining thresholds. Nothing is ever retrained or promoted here."""
    with MONITORING_LOCK:
        try:
            policy = monitoring.save_policy(MONITORING_POLICY_PATH, payload.model_dump(exclude_none=True))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"policy": policy, "monitoring": monitoring_payload()}


def _without_empty_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep stored secrets when the browser submits the redacted empty values."""
    cleaned: dict[str, Any] = {
        section: dict(values) if isinstance(values, dict) else values for section, values in (payload or {}).items()
    }
    for section, fields in notifications.SECRET_FIELDS.items():
        block = cleaned.get(section)
        if isinstance(block, dict):
            for field in fields:
                if block.get(field) == "":
                    block.pop(field, None)
    return cleaned


@app.get("/api/notifications", dependencies=[Depends(authorize)])
def list_notifications(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    """In-app notifications plus the current (secret-free) channel policy."""
    items = notifications.read_notifications(NOTIFICATION_STORE_PATH, limit=limit)
    return api_safe(
        {
            "notifications": items,
            "summary": notifications.summarize(items),
            "policy": notifications.public_policy(notifications.load_policy(NOTIFICATION_POLICY_PATH)),
        }
    )


@app.get("/api/notifications/policy", dependencies=[Depends(authorize)])
def get_notification_policy() -> dict:
    return {"policy": notifications.public_policy(notifications.load_policy(NOTIFICATION_POLICY_PATH))}


@app.put("/api/notifications/policy", dependencies=[Depends(authorize)])
def update_notification_policy(payload: dict[str, Any] = Body(...)) -> dict:
    """Save notification channels and thresholds. Empty secrets keep the stored value."""
    with NOTIFICATION_LOCK:
        try:
            policy = notifications.save_policy(NOTIFICATION_POLICY_PATH, _without_empty_secrets(payload))
        except notifications.NotificationConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Could not save the notification policy") from exc
    return {"policy": notifications.public_policy(policy)}


@app.post("/api/notifications/evaluate", dependencies=[Depends(authorize)])
def evaluate_notifications() -> dict:
    """Evaluate thresholds now, store in-app notifications, and deliver configured channels."""
    policy = notifications.load_policy(NOTIFICATION_POLICY_PATH)
    channels = ("in_app", "email", "webhook")
    if not any(policy[channel]["enabled"] for channel in channels):
        return {
            "evaluated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "notifications": [],
            "summary": notifications.summarize([]),
            "delivery": {"in_app": 0, "email": {"sent": 0, "failed": 0, "errors": []}, "webhook": {"sent": 0, "failed": 0, "errors": []}},
            "message": "All notification channels are disabled.",
        }
    with NOTIFICATION_LOCK:
        snapshot = monitoring_payload()
        data = read_daily_data()
        forecast = read_forecast_frame()
        alerts: list[dict] = []
        if data is not None and forecast is not None:
            alerts = decisions.explain_alerts(
                data,
                forecast,
                agency=str(snapshot.get("source") or "operator"),
                capacity_config=read_capacity_assumptions(),
            )
        items = notifications.build_notifications(alerts, snapshot.get("warnings") or [], policy, snapshot)
        delivery = notifications.deliver(items, policy)
        stored = {"added": 0, "skipped": 0}
        if policy["in_app"]["enabled"]:
            stored = notifications.append_notifications(
                NOTIFICATION_STORE_PATH, items, max_items=int(policy["in_app"]["max_items"])
            )
        elif items:
            stored = {"added": 0, "skipped": len(items)}
    return api_safe(
        {
            "evaluated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "alerts_evaluated": len(alerts),
            "notifications": items,
            "summary": notifications.summarize(items),
            "delivery": delivery,
            "stored": stored,
            "message": "Notification run complete. Nothing is retrained and no operational change is made.",
        }
    )


@app.post("/api/notifications/{notification_id}/acknowledge", dependencies=[Depends(authorize)])
def acknowledge_notification(notification_id: str) -> dict:
    with NOTIFICATION_LOCK:
        if not notifications.acknowledge_notification(NOTIFICATION_STORE_PATH, notification_id):
            raise HTTPException(status_code=404, detail="Notification not found")
    return {"acknowledged": notification_id}


@app.get("/api/reports", dependencies=[Depends(authorize)])
def list_reports() -> dict:
    inputs = reports_inputs()
    return api_safe(
        {
            "reports": reports.catalog(**inputs),
            "capacity_configured_routes": sorted(inputs["capacity"]),
            "notice": reports.NOTICE,
        }
    )


@app.get("/api/reports/capacity", dependencies=[Depends(authorize)])
def get_capacity_assumptions() -> dict:
    return {"capacity": read_capacity_assumptions(), "fields": list(reports.CAPACITY_FIELDS)}


@app.put("/api/reports/capacity", dependencies=[Depends(authorize)])
def update_capacity_assumptions(payload: dict[str, Any] = Body(...)) -> dict:
    candidate = payload.get("capacity", payload) if isinstance(payload, dict) else payload
    try:
        normalized = reports.normalize_capacity_assumptions(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    temporary = CAPACITY_ASSUMPTIONS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(CAPACITY_ASSUMPTIONS_PATH)
    return {"capacity": normalized}


def _report_or_404(report_id: str) -> dict:
    if report_id not in reports.REPORT_IDS:
        raise HTTPException(status_code=404, detail="Unknown report")
    report = api_safe(reports.build_report(report_id, **reports_inputs()))
    if not report.get("available"):
        raise HTTPException(status_code=404, detail=report.get("unavailable_reason") or "Report unavailable")
    return report


@app.get("/api/reports/{report_id}.csv", dependencies=[Depends(authorize)])
def report_csv(report_id: str) -> Response:
    report = _report_or_404(report_id)
    return Response(
        content=reports.report_to_csv(report),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="bus_demand_{report_id}.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/reports/{report_id}.html", dependencies=[Depends(authorize)])
def report_html(report_id: str) -> Response:
    report = _report_or_404(report_id)
    return Response(
        content=reports.report_to_html(report),
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


def security_failure(exc: security.SecurityError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.get("/api/security/me", dependencies=[Depends(authorize)])
def security_identity(principal: security.Principal = Depends(authorize)) -> dict:
    """Identity, role permissions, and rate-limit policy for the current caller."""
    registry = security_registry()
    return {
        "principal": security.public_principal(principal),
        "workspace_agency": registry["workspace_agency"],
        "auth_required": auth_required(),
        "rate_limit_per_minute": rate_limit_per_minute(),
        "roles": {role: sorted(permissions) for role, permissions in security.ROLE_PERMISSIONS.items()},
        "policy": [
            {"method": method, "path": pattern, "permission": permission}
            for method, pattern, permission in SECURITY_POLICY
        ],
    }


@app.get("/api/security/agencies", dependencies=[Depends(authorize)])
def security_agencies() -> dict:
    registry = security_registry()
    return {
        "workspace_agency": registry["workspace_agency"],
        "agencies": security.public_agencies(registry),
    }


@app.post("/api/security/agencies", dependencies=[Depends(authorize)])
def create_security_agency(payload: dict = Body(...), principal: security.Principal = Depends(authorize)) -> dict:
    with SECURITY_LOCK:
        registry = security_registry()
        try:
            agency = security.create_agency(registry, str(payload.get("code") or ""), str(payload.get("name") or ""))
        except security.SecurityError as exc:
            raise security_failure(exc) from exc
        security.save_registry(SECURITY_REGISTRY_PATH, registry)
    audit_security(action="agency:create", outcome="allowed", principal=principal, target=agency["code"])
    return {"agency": agency, "agencies": security.public_agencies(registry)}


@app.get("/api/security/principals", dependencies=[Depends(authorize)])
def security_principals() -> dict:
    registry = security_registry()
    return {
        "workspace_agency": registry["workspace_agency"],
        "principals": security.public_principals(registry),
    }


@app.post("/api/security/principals", dependencies=[Depends(authorize)])
def create_security_principal(payload: dict = Body(...), principal: security.Principal = Depends(authorize)) -> dict:
    """Create an API principal. The API key is returned exactly once."""
    with SECURITY_LOCK:
        registry = security_registry()
        try:
            record, api_key = security.create_principal(
                registry,
                str(payload.get("name") or ""),
                str(payload.get("role") or ""),
                str(payload.get("agency") or ""),
            )
        except security.SecurityError as exc:
            raise security_failure(exc) from exc
        security.save_registry(SECURITY_REGISTRY_PATH, registry)
    audit_security(
        action="principal:create",
        outcome="allowed",
        principal=principal,
        target=record["id"],
        detail={"name": record["name"], "role": record["role"], "agency": record["agency"]},
    )
    return {
        "principal": security.public_principals(registry)[-1],
        "api_key": api_key,
        "notice": "Store this API key now. It is not shown again; rotate it if it is lost.",
    }


@app.post("/api/security/principals/{principal_id}/rotate", dependencies=[Depends(authorize)])
def rotate_security_principal(principal_id: str, principal: security.Principal = Depends(authorize)) -> dict:
    with SECURITY_LOCK:
        registry = security_registry()
        try:
            record, api_key = security.rotate_principal_key(registry, principal_id)
        except security.SecurityError as exc:
            raise security_failure(exc) from exc
        security.save_registry(SECURITY_REGISTRY_PATH, registry)
    audit_security(action="principal:rotate", outcome="allowed", principal=principal, target=principal_id)
    return {
        "principal_id": record["id"],
        "api_key": api_key,
        "notice": "The previous key stops working immediately. Store this key now.",
    }


@app.post("/api/security/principals/{principal_id}/deactivate", dependencies=[Depends(authorize)])
def deactivate_security_principal(principal_id: str, principal: security.Principal = Depends(authorize)) -> dict:
    with SECURITY_LOCK:
        registry = security_registry()
        try:
            record = security.find_principal_record(registry, principal_id)
            if record.get("id") == principal.id:
                raise security.InputValidationError("Administrators cannot deactivate their own principal")
            active_admins = [
                item for item in registry.get("principals", [])
                if item.get("role") == security.ROLE_ADMIN and item.get("active", True)
            ]
            if record.get("role") == security.ROLE_ADMIN and len(active_admins) <= 1:
                raise security.InputValidationError("At least one active administrator is required")
            security.deactivate_principal(registry, principal_id)
        except security.SecurityError as exc:
            raise security_failure(exc) from exc
        security.save_registry(SECURITY_REGISTRY_PATH, registry)
    audit_security(action="principal:deactivate", outcome="allowed", principal=principal, target=principal_id)
    return {"principal_id": principal_id, "active": False}


@app.get("/api/security/audit", dependencies=[Depends(authorize)])
def security_audit(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    return {"events": security.read_audit(SECURITY_AUDIT_PATH, limit)}


@app.get("/api/public-demo/status")
def public_demo_status(days: int = Query(default=7, ge=1, le=28)) -> dict:
    """Return a cached, read-only synthetic forecast without touching operator data."""
    global PUBLIC_DEMO_CACHE
    if PUBLIC_DEMO_CACHE is not None:
        return public_demo_forecast_for_days(days)
    with PUBLIC_DEMO_LOCK:
        if PUBLIC_DEMO_CACHE is not None:
            return public_demo_forecast_for_days(days)
        sample = project_path("data/sample_daily_ridership.csv")
        if not sample.exists():
            raise HTTPException(status_code=500, detail="The bundled synthetic dataset is missing")
        demo_dir = Path(tempfile.mkdtemp(prefix="bus-forecast-public-demo-"))
        demo_data_path = demo_dir / "daily_ridership.csv"
        model_path = demo_dir / "model.joblib"
        metrics_path = demo_dir / "metrics.json"
        predictions_path = demo_dir / "predictions.csv"
        holdout_path = demo_dir / "holdout_predictions.csv"
        try:
            data = load_data(str(sample))
            original_dates = pd.DatetimeIndex(sorted(data["service_date"].unique()))
            last_observed_date = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None) - pd.Timedelta(days=1)
            shifted_dates = pd.date_range(end=last_observed_date, periods=len(original_dates), freq="D")
            date_map = dict(zip(original_dates, shifted_dates))
            data["service_date"] = data["service_date"].map(date_map)
            data.to_csv(demo_data_path, index=False)
            training = train_project(
                data_path=demo_data_path,
                model_out=model_path,
                horizon=7,
                folds=4,
                metrics_out=metrics_path,
                predictions_out=predictions_path,
                holdout_predictions_out=holdout_path,
            )
            data = load_data(str(demo_data_path))
            forecasts = build_forecast(demo_data_path, model_path, 7, "error")
            PUBLIC_DEMO_ARTIFACTS.update({"data": demo_data_path, "model": model_path})
            PUBLIC_DEMO_CACHE = {
                "data_loaded": True,
                "source": "synthetic demo data",
                "rows": int(len(data)),
                "route_count": int(data["route_id"].nunique()),
                "latest_observed_date": data["service_date"].max().date().isoformat(),
                "model_trained": True,
                "trained_through": training["trained_through"],
                "horizon_days": 7,
                "metrics": json.loads(metrics_path.read_text(encoding="utf-8")),
                "forecasts": forecasts.to_dict(orient="records"),
                "forecast_updated_at": training["created_at_utc"],
                "public_demo": True,
            }
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail="Could not build the synthetic public preview") from exc
    return public_demo_forecast_for_days(days)


def public_demo_forecast_for_days(days: int) -> dict:
    """Return a read-only forecast horizon from the cached synthetic model."""
    if PUBLIC_DEMO_CACHE is None:
        raise HTTPException(status_code=503, detail="The synthetic demo model is not ready")
    if days == PUBLIC_DEMO_CACHE["horizon_days"]:
        return PUBLIC_DEMO_CACHE
    data_path = PUBLIC_DEMO_ARTIFACTS.get("data")
    model_path = PUBLIC_DEMO_ARTIFACTS.get("model")
    if data_path is None or model_path is None:
        raise HTTPException(status_code=503, detail="The synthetic demo model is not ready")
    try:
        forecast = build_forecast(data_path, model_path, days, "error")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Could not generate the synthetic forecast horizon") from exc
    return {**PUBLIC_DEMO_CACHE, "horizon_days": days, "forecasts": forecast.to_dict(orient="records")}


@app.get("/api/public-demo/forecast.csv")
def public_demo_forecast_csv(
    days: int = Query(default=7, ge=1, le=28),
    route_id: str | None = Query(default=None, min_length=1, max_length=128),
) -> Response:
    payload = public_demo_status(days)
    forecast = pd.DataFrame(payload["forecasts"])
    if route_id:
        forecast = forecast.loc[forecast["route_id"].astype(str) == route_id]
        if forecast.empty:
            raise HTTPException(status_code=404, detail="That route is not in the synthetic demo")
    return Response(
        content=forecast.to_csv(index=False),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bus_demand_synthetic_demo_forecast.csv"'},
    )


@app.get("/api/public-demo/sample.csv")
def public_demo_sample_csv() -> Response:
    payload = public_demo_status(7)
    sample = project_path("data/sample_daily_ridership.csv")
    if not sample.exists():
        raise HTTPException(status_code=500, detail="The bundled synthetic dataset is missing")
    data = load_data(str(sample))
    original_dates = pd.DatetimeIndex(sorted(data["service_date"].unique()))
    latest_date = pd.Timestamp(payload["latest_observed_date"])
    shifted_dates = pd.date_range(end=latest_date, periods=len(original_dates), freq="D")
    data["service_date"] = data["service_date"].map(dict(zip(original_dates, shifted_dates)))
    return Response(
        content=data.to_csv(index=False, date_format="%Y-%m-%d"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="synthetic_daily_route_counts.csv"'},
    )


@app.post("/api/data/upload", dependencies=[Depends(authorize), Depends(protect_pipeline)])
def upload_data(
    file: UploadFile = File(...),
    missing_days: Literal["error", "zero"] = Form(default="error"),
) -> dict:
    if not file.filename or Path(file.filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=415, detail="Upload a CSV file")
    try:
        security.validate_upload_name(file.filename, allowed_extensions=frozenset({".csv"}))
    except security.InputValidationError as exc:
        raise HTTPException(status_code=415, detail=exc.detail) from exc
    contents = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="CSV upload exceeds the 20 MB limit")
    if not contents.strip():
        raise HTTPException(status_code=422, detail="The uploaded CSV is empty")
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as temporary_file:
        temporary_path = Path(temporary_file.name)
        temporary_file.write(contents)
    imported_schedule = False
    try:
        try:
            headers = set(pd.read_csv(temporary_path, nrows=0, encoding="utf-8-sig").columns)
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            raise HTTPException(status_code=422, detail="The CSV could not be read. Save it as UTF-8 CSV and try again.") from exc
        if REQUIRED_COLUMNS.issubset(headers):
            try:
                data = load_data(str(temporary_path))
                complete_daily_series(data, missing_days=missing_days)
            except (UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        else:
            try:
                data = schedules_to_synthetic_counts(contents)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            imported_schedule = True
            contents = data.to_csv(index=False).encode("utf-8")
        replace_path = DATA_PATH.with_suffix(".uploading")
        replace_path.write_bytes(contents)
        replace_path.replace(DATA_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)
    if imported_schedule:
        for artifact in (MODEL_PATH, METRICS_PATH, PREDICTIONS_PATH, HOLDOUT_PREDICTIONS_PATH, FORECAST_PATH, ARTIFACT_DIR / "error_analysis.json"):
            artifact.unlink(missing_ok=True)
    source = "synthetic schedule demo data" if imported_schedule else "company upload"
    source_system = "synthetic_schedule_scenario" if imported_schedule else "manual_csv"
    write_metadata(source, source_system, len(data), int(data["route_id"].nunique()))
    record_audit_event("synthetic_schedule_import" if imported_schedule else "replace_upload", source_system, len(data), int(data["route_id"].nunique()))
    if imported_schedule:
        return {
            "message": f"Timetable accepted for {int(data['route_id'].nunique())} routes. Generated 730 days of synthetic demonstration counts from schedule frequency; these are NOT measured passenger counts. Train the model to view the illustrative forecast.",
            "status": status_payload(),
        }
    forecast_message = "Data saved. Train a model to create the first forecast."
    if MODEL_PATH.exists():
        try:
            forecast = build_forecast(DATA_PATH, MODEL_PATH, min(7, int(status_payload().get("horizon_days") or 7)), missing_days)
            forecast.to_csv(FORECAST_PATH.with_suffix(".tmp"), index=False)
            FORECAST_PATH.with_suffix(".tmp").replace(FORECAST_PATH)
            forecast_message = "Data saved and the forecast refreshed using the current model."
        except ValueError as exc:
            FORECAST_PATH.unlink(missing_ok=True)
            forecast_message = f"Data saved. Retrain the model before forecasting: {exc}"
    return {"message": forecast_message, "status": status_payload()}


@app.post("/api/data/ingest", dependencies=[Depends(authorize), Depends(protect_pipeline)])
def ingest_daily_counts(request: DataIngestRequest) -> dict:
    """Idempotently upsert daily counts from a scheduled company export or integration."""
    try:
        security.ensure_no_passenger_identifiers([record.model_dump() for record in request.records])
        for record in request.records:
            security.validate_route_id(record.route_id)
            security.validate_iso_date(record.service_date)
    except security.InputValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    source_system = request.source_system.strip()
    if not source_system:
        raise HTTPException(status_code=422, detail="source_system cannot be blank")
    incoming = pd.DataFrame([record.model_dump() for record in request.records])
    incoming["service_date"] = pd.to_datetime(incoming["service_date"], errors="coerce").dt.normalize()
    incoming["route_id"] = incoming["route_id"].astype("string").str.strip()
    incoming["passengers"] = pd.to_numeric(incoming["passengers"], errors="coerce")
    if incoming["service_date"].isna().any():
        raise HTTPException(status_code=422, detail="service_date must use YYYY-MM-DD")
    if incoming["route_id"].isna().any() or incoming["route_id"].eq("").any():
        raise HTTPException(status_code=422, detail="route_id cannot be blank")
    if incoming["passengers"].isna().any() or not incoming["passengers"].map(lambda value: math.isfinite(float(value))).all():
        raise HTTPException(status_code=422, detail="passengers must be finite numbers")
    if (incoming["passengers"] < 0).any():
        raise HTTPException(status_code=422, detail="passengers cannot be negative")
    keys = ["service_date", "route_id"]
    if incoming.duplicated(keys).any():
        raise HTTPException(status_code=422, detail="Batch contains duplicate service_date/route_id records")

    try:
        current = load_data(str(DATA_PATH)) if DATA_PATH.exists() else pd.DataFrame(columns=["service_date", "route_id", "passengers"])
        if source_system == "manual_daily_entry":
            known_route_ids = set(current["route_id"].astype(str))
            canonical_routes = []
            for route_id in incoming["route_id"].astype(str):
                canonical_route = canonicalize_manual_route_id(route_id, known_route_ids)
                canonical_routes.append(canonical_route)
                known_route_ids.add(canonical_route)
            incoming["route_id"] = canonical_routes
            if incoming.duplicated(keys).any():
                raise HTTPException(status_code=422, detail="Batch contains duplicate service_date/route records after route-name normalization")
        existing_keys = set(zip(current["service_date"], current["route_id"]))
        incoming_keys = set(zip(incoming["service_date"], incoming["route_id"]))
        inserted_count = len(incoming_keys - existing_keys)
        updated_count = len(incoming_keys & existing_keys)
        merged = pd.concat([current, incoming], ignore_index=True).drop_duplicates(keys, keep="last")
        merged = merged.sort_values(keys).reset_index(drop=True)
        merged = complete_daily_series(merged, missing_days=request.missing_days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    persist_daily_data(merged)
    route_count = int(merged["route_id"].nunique())
    write_metadata("company feed", source_system, len(merged), route_count)
    record_audit_event("upsert", source_system, len(incoming), route_count)
    forecast_message = "Counts ingested. Train a model to create the first forecast."
    if MODEL_PATH.exists():
        try:
            horizon = min(90, int(status_payload().get("horizon_days") or 7))
            forecast = build_forecast(DATA_PATH, MODEL_PATH, horizon, request.missing_days)
            temporary = FORECAST_PATH.with_suffix(".tmp")
            forecast.to_csv(temporary, index=False)
            temporary.replace(FORECAST_PATH)
            forecast_message = "Counts ingested and the forecast refreshed using the current model."
        except ValueError as exc:
            FORECAST_PATH.unlink(missing_ok=True)
            forecast_message = f"Counts ingested. Retrain the model before forecasting: {exc}"
    return {
        "message": forecast_message,
        "inserted_records": inserted_count,
        "updated_records": updated_count,
        "status": status_payload(),
    }


@app.post("/api/data/demo", dependencies=[Depends(authorize), Depends(protect_pipeline)])
def load_demo_data() -> dict:
    sample = project_path("data/sample_daily_ridership.csv")
    if not sample.exists():
        raise HTTPException(status_code=500, detail="The bundled sample dataset is missing")
    data = load_data(str(sample))
    complete_daily_series(data)
    replace_path = DATA_PATH.with_suffix(".uploading")
    replace_path.write_bytes(sample.read_bytes())
    replace_path.replace(DATA_PATH)
    MODEL_PATH.unlink(missing_ok=True)
    FORECAST_PATH.unlink(missing_ok=True)
    write_metadata("synthetic demo data", "bundled_sample", len(data), int(data["route_id"].nunique()))
    record_audit_event("load_synthetic_demo", "bundled_sample", len(data), int(data["route_id"].nunique()))
    return {"message": "Synthetic demo data loaded. Train it to generate a sample forecast.", "status": status_payload()}


@app.post("/api/train", dependencies=[Depends(authorize), Depends(protect_pipeline)])
def train_model(request: TrainRequest) -> dict:
    if not DATA_PATH.exists():
        raise HTTPException(status_code=409, detail="Upload daily route counts or load demo data first")
    try:
        result = train_project(
            data_path=DATA_PATH,
            model_out=MODEL_PATH,
            horizon=request.horizon,
            folds=request.folds,
            missing_days=request.missing_days,
            metrics_out=METRICS_PATH,
            predictions_out=PREDICTIONS_PATH,
            holdout_predictions_out=HOLDOUT_PREDICTIONS_PATH,
        )
        save_forecast(request.horizon, request.missing_days)
        trained_data = load_data(str(DATA_PATH))
        record_audit_event("train_model", read_metadata().get("source_system", "unknown"), len(trained_data), int(trained_data["route_id"].nunique()))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"message": "Model trained and forecast updated.", "training": api_safe(result), "status": status_payload()}


@app.post("/api/forecast", dependencies=[Depends(authorize), Depends(protect_pipeline)])
def refresh_forecast(request: ForecastRequest) -> dict:
    forecast = save_forecast(request.days, request.missing_days)
    return {"message": "Forecast refreshed.", "rows": int(len(forecast)), "status": status_payload()}


@app.get("/api/forecast.csv", dependencies=[Depends(authorize)])
def download_forecast(route_id: str | None = Query(default=None, min_length=1, max_length=128)) -> Response:
    if not FORECAST_PATH.exists():
        raise HTTPException(status_code=404, detail="No forecast has been generated yet")
    forecast = pd.read_csv(FORECAST_PATH)
    if route_id:
        forecast = forecast.loc[forecast["route_id"].astype(str) == route_id]
        if forecast.empty:
            raise HTTPException(status_code=404, detail="That route is not in the current forecast")
    return Response(
        content=forecast.to_csv(index=False),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bus_demand_forecast.csv"'},
    )


@app.get("/api/template.csv", dependencies=[Depends(authorize)])
def download_template() -> Response:
    return FileResponse(project_path("data/sample_daily_ridership.csv"), media_type="text/csv", filename="daily_route_counts_template.csv")


# ---------------------------------------------------------------------------
# Monthly passenger data (independent from the daily forecasting dataset)
# ---------------------------------------------------------------------------

def _monthly_store() -> dict:
    try:
        return load_monthly_store(MONTHLY_STORE_PATH)
    except MonthlyDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _monthly_routes_payload() -> dict:
    store = _monthly_store()
    totals = store.get("totals") or {}
    return {
        "dataset": MONTHLY_DATASET_NAME,
        "row_count": int(totals.get("rows") or 0),
        "route_count": int(totals.get("routes") or 0),
        "first_month": totals.get("first_month"),
        "last_month": totals.get("last_month"),
        "routes": list_monthly_routes(store),
    }


def _monthly_history_payload(route_id: str) -> dict:
    history = get_monthly_route_history(_monthly_store(), route_id)
    if history is None:
        raise HTTPException(status_code=404, detail=f"No monthly data is stored for route {route_id}")
    return history


@app.post("/api/monthly-data/upload", dependencies=[Depends(authorize), Depends(protect_monthly_store)])
def upload_monthly_data(file: UploadFile = File(...)) -> dict:
    """Merge a monthly boardings CSV into the separate monthly store."""
    if not file.filename or Path(file.filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=415, detail="Upload a CSV file")
    try:
        security.validate_upload_name(file.filename, allowed_extensions=frozenset({".csv"}))
    except security.InputValidationError as exc:
        raise HTTPException(status_code=415, detail=exc.detail) from exc
    contents = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="CSV upload exceeds the 20 MB limit")
    if not contents.strip():
        raise HTTPException(status_code=422, detail="The uploaded CSV is empty")
    try:
        result = ingest_monthly_csv(
            MONTHLY_STORE_PATH,
            MONTHLY_AUDIT_PATH,
            contents,
            file_name=Path(file.filename).name,
        )
    except MonthlyDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "message": (
            "Monthly boardings saved to the independent monthly store. Missing route-months stay absent: "
            "no values were filled, zeroed, or interpolated."
        ),
        "import": result["import"],
        "status": result["status"],
    }


@app.get("/api/monthly-data/status", dependencies=[Depends(authorize)])
def monthly_data_status() -> dict:
    payload = build_monthly_status(_monthly_store())
    payload["recent_activity"] = read_monthly_audit(MONTHLY_AUDIT_PATH)
    return payload


@app.get("/api/monthly-data/routes", dependencies=[Depends(authorize)])
def monthly_data_routes() -> dict:
    return _monthly_routes_payload()


@app.get("/api/monthly-data/routes/{route_id}/history", dependencies=[Depends(authorize)])
def monthly_data_route_history(route_id: str) -> dict:
    return _monthly_history_payload(route_id)


@app.get("/api/public/monthly-data/routes", dependencies=[Depends(require_public_monthly_read)])
def public_monthly_data_routes() -> dict:
    """Public read-only monthly route index; disabled unless enabled by the operator."""
    return _monthly_routes_payload()


@app.get("/api/public/monthly-data/routes/{route_id}/history", dependencies=[Depends(require_public_monthly_read)])
def public_monthly_data_route_history(route_id: str) -> dict:
    """Public read-only monthly history; disabled unless enabled by the operator."""
    return _monthly_history_payload(route_id)


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/ready")
def readiness() -> JSONResponse:
    """Readiness probe: database reachability plus the applied schema revision."""
    from .db import check_database, migration_status

    database = check_database()
    migrations = migration_status()
    ready = bool(database["reachable"]) and not migrations["pending"]
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"ready": ready, "database": database, "migrations": migrations},
    )


@app.get("/api/migrations", dependencies=[Depends(authorize)])
def migration_report() -> dict:
    """Operator view of the database revision and migration state."""
    from .db import check_database, migration_status

    return {"database": check_database(), "migrations": migration_status()}


@app.get("/api/storage/health", dependencies=[Depends(authorize)])
def storage_report() -> dict:
    """Operator view of data-directory writability, space, and key artifacts."""
    from .db import storage_health

    return storage_health(DATA_DIR)


# ---------------------------------------------------------------------------
# GTFS Schedule (static transit network)
# ---------------------------------------------------------------------------


def protect_gtfs_import():
    """Serialize GTFS imports separately from daily and monthly writes."""
    if not GTFS_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A GTFS import is already in progress")
    try:
        yield
    finally:
        GTFS_LOCK.release()


def _gtfs_session_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="The transit database is not ready. Run the database migration and try again.",
    )


@app.post("/api/gtfs/upload", dependencies=[Depends(authorize), Depends(protect_gtfs_import)])
def upload_gtfs(file: UploadFile = File(...), principal: security.Principal = Depends(authorize)) -> dict:
    """Validate and import a GTFS Schedule ZIP as a new feed version."""
    if principal.role != security.ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="GTFS feed imports change the shared network catalog and require the administrator role",
        )

    from .db import session_scope
    from .gtfs_static import GtfsValidationError, import_feed

    if not file.filename or Path(file.filename).suffix.lower() != ".zip":
        raise HTTPException(status_code=415, detail="Upload a .zip GTFS Schedule feed")
    try:
        security.validate_upload_name(file.filename, allowed_extensions=frozenset({".zip"}))
    except security.InputValidationError as exc:
        raise HTTPException(status_code=415, detail=exc.detail) from exc
    contents = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Feed exceeds the 20 MB upload limit")
    try:
        security.inspect_zip_payload(contents, max_bytes=MAX_UPLOAD_BYTES)
    except security.InputValidationError as exc:
        # Keep malformed archives on the same structured validation contract
        # as GTFS content errors returned by parse_feed().
        raise HTTPException(
            status_code=422,
            detail={"message": "Invalid GTFS upload", "issues": [exc.detail]},
        ) from exc
    checksum = hashlib.sha256(contents).hexdigest()
    try:
        with session_scope() as session:
            summary = import_feed(session, contents, file.filename, checksum)
    except GtfsValidationError as exc:
        raise HTTPException(status_code=422, detail={"message": "Invalid GTFS feed", "issues": exc.issues}) from exc
    except Exception as exc:  # database unavailable or schema missing
        raise _gtfs_session_error(exc) from exc
    return {
        "message": (
            "GTFS feed imported. The new version is now the active catalog for this agency; "
            "previous versions are retained for history."
        ),
        **summary,
        "checksum": checksum,
    }


@app.get("/api/gtfs/feeds", dependencies=[Depends(authorize)])
def list_gtfs_feeds(agency: str | None = Query(default=None, max_length=64)) -> dict:
    from sqlalchemy import select

    from .db import session_scope
    from .schema import GtfsFeed

    with session_scope() as session:
        statement = select(GtfsFeed).order_by(GtfsFeed.id.desc()).limit(50)
        if agency:
            statement = statement.where(GtfsFeed.agency_code == agency.strip().upper())
        feeds = [
            {
                "feed_id": feed.id,
                "agency_code": feed.agency_code,
                "file_name": feed.file_name,
                "checksum": feed.checksum,
                "feed_start": str(feed.feed_start) if feed.feed_start else None,
                "feed_end": str(feed.feed_end) if feed.feed_end else None,
                "routes": feed.route_count,
                "stops": feed.stop_count,
                "trips": feed.trip_count,
                "stop_times": feed.stop_time_count,
                "status": feed.status,
                "imported_at": feed.created_at.isoformat() if feed.created_at else None,
            }
            for feed in session.execute(statement).scalars()
        ]
    return {"feeds": feeds, "count": len(feeds)}


@app.get("/api/gtfs/agencies", dependencies=[Depends(authorize)])
def list_gtfs_agencies() -> dict:
    from sqlalchemy import func, select

    from .db import session_scope
    from .gtfs_static import active_feed_id
    from .schema import GtfsFeed, GtfsRoute, GtfsStop

    with session_scope() as session:
        codes = session.execute(select(GtfsFeed.agency_code).distinct()).scalars().all()
        agencies = []
        for code in codes:
            feed_id = active_feed_id(session, code)
            feed = session.get(GtfsFeed, feed_id) if feed_id else None
            routes = session.scalar(select(func.count()).select_from(GtfsRoute).where(GtfsRoute.feed_id == feed_id))
            stops = session.scalar(select(func.count()).select_from(GtfsStop).where(GtfsStop.feed_id == feed_id))
            agencies.append(
                {
                    "agency_code": code,
                    "active_feed_id": feed_id,
                    "feed_start": str(feed.feed_start) if feed and feed.feed_start else None,
                    "feed_end": str(feed.feed_end) if feed and feed.feed_end else None,
                    "route_count": int(routes or 0),
                    "stop_count": int(stops or 0),
                }
            )
    return {"agencies": agencies, "count": len(agencies)}


@app.get("/api/gtfs/routes", dependencies=[Depends(authorize)])
def list_gtfs_routes(
    agency: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    from sqlalchemy import func, or_, select

    from .db import session_scope
    from .gtfs_static import active_feed_id
    from .schema import GtfsFeed, GtfsRoute

    with session_scope() as session:
        if agency:
            codes = [agency.strip().upper()]
        else:
            codes = session.execute(select(GtfsFeed.agency_code).distinct()).scalars().all()
        items: list[dict] = []
        total = 0
        for code in codes:
            feed_id = active_feed_id(session, code)
            if feed_id is None:
                continue
            statement = select(GtfsRoute).where(GtfsRoute.feed_id == feed_id)
            if q:
                pattern = f"%{q.strip()}%"
                statement = statement.where(
                    or_(GtfsRoute.route_id.ilike(pattern), GtfsRoute.short_name.ilike(pattern), GtfsRoute.long_name.ilike(pattern))
                )
            total += int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            for route in session.execute(statement.order_by(GtfsRoute.short_name, GtfsRoute.route_id).limit(limit).offset(offset)).scalars():
                items.append(
                    {
                        "agency_code": route.agency_code,
                        "route_id": route.route_id,
                        "short_name": route.short_name,
                        "long_name": route.long_name,
                        "route_type": route.route_type,
                        "color": route.color,
                        "feed_id": route.feed_id,
                    }
                )
    return {"routes": items[:limit], "count": len(items[:limit]), "total": total, "limit": limit, "offset": offset}


@app.get("/api/gtfs/routes/{route_id}", dependencies=[Depends(authorize)])
def gtfs_route_detail(route_id: str, agency: str | None = Query(default=None, max_length=64)) -> dict:
    from sqlalchemy import func, select

    from .db import session_scope
    from .gtfs_static import active_feed_id
    from .schema import GtfsFeed, GtfsRoute, GtfsStop, GtfsStopTime, GtfsTrip

    with session_scope() as session:
        codes = [agency.strip().upper()] if agency else session.execute(select(GtfsFeed.agency_code).distinct()).scalars().all()
        for code in codes:
            feed_id = active_feed_id(session, code)
            if feed_id is None:
                continue
            route = session.scalar(
                select(GtfsRoute).where(GtfsRoute.feed_id == feed_id, GtfsRoute.route_id == route_id)
            )
            if route is None:
                continue
            trip_ids = session.execute(
                select(GtfsTrip.trip_id).where(GtfsTrip.feed_id == feed_id, GtfsTrip.route_id == route_id)
            ).scalars().all()
            trip_count = len(trip_ids)
            times = session.execute(
                select(
                    func.min(GtfsStopTime.departure_time),
                    func.max(GtfsStopTime.departure_time),
                ).where(GtfsStopTime.feed_id == feed_id, GtfsStopTime.trip_id.in_(trip_ids))
            ).one() if trip_ids else (None, None)
            stop_rows = session.execute(
                select(GtfsStop)
                .where(
                    GtfsStop.feed_id == feed_id,
                    GtfsStop.stop_id.in_(
                        select(GtfsStopTime.stop_id).where(
                            GtfsStopTime.feed_id == feed_id, GtfsStopTime.trip_id.in_(trip_ids)
                        )
                    ),
                )
                .distinct()
                .order_by(GtfsStop.name)
                .limit(500)
            ).scalars().all()
            return {
                "agency_code": code,
                "feed_id": feed_id,
                "route_id": route.route_id,
                "short_name": route.short_name,
                "long_name": route.long_name,
                "route_type": route.route_type,
                "color": route.color,
                "trip_count": trip_count,
                "first_departure": times[0],
                "last_departure": times[1],
                "stop_count": len(stop_rows),
                "stops": [
                    {
                        "stop_id": stop.stop_id,
                        "name": stop.name,
                        "latitude": stop.latitude,
                        "longitude": stop.longitude,
                    }
                    for stop in stop_rows
                ],
                "geometry_note": "Stop coordinates come from the imported feed; no positions are inferred.",
            }
    raise HTTPException(status_code=404, detail=f"Route {route_id} is not in the imported network")


@app.get("/api/gtfs/stops", dependencies=[Depends(authorize)])
def list_gtfs_stops(
    agency: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    from sqlalchemy import func, select

    from .db import session_scope
    from .gtfs_static import active_feed_id
    from .schema import GtfsFeed, GtfsStop

    with session_scope() as session:
        codes = [agency.strip().upper()] if agency else session.execute(select(GtfsFeed.agency_code).distinct()).scalars().all()
        items: list[dict] = []
        total = 0
        for code in codes:
            feed_id = active_feed_id(session, code)
            if feed_id is None:
                continue
            statement = select(GtfsStop).where(GtfsStop.feed_id == feed_id)
            if q:
                statement = statement.where(GtfsStop.name.ilike(f"%{q.strip()}%"))
            total += int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            for stop in session.execute(statement.order_by(GtfsStop.name).limit(limit).offset(offset)).scalars():
                items.append(
                    {
                        "agency_code": stop.agency_code,
                        "stop_id": stop.stop_id,
                        "name": stop.name,
                        "latitude": stop.latitude,
                        "longitude": stop.longitude,
                    }
                )
    return {"stops": items[:limit], "count": len(items[:limit]), "total": total, "limit": limit, "offset": offset}


@app.exception_handler(HTTPException)
async def api_error_handler(_request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


