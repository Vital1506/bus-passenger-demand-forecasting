"""Local and hosted API for the Bus Passenger Demand Forecast app."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import (
    check_db_health,
    check_storage_health,
    get_db_session,
    get_migration_status,
    init_db,
    record_audit_event as repo_record_audit_event,
    record_monthly_import_event as repo_record_monthly_import_event,
    replace_all_daily_counts,
    get_monthly_observations as repo_get_monthly_observations,
    get_monthly_route_history as repo_get_monthly_route_history,
    upsert_monthly_records as repo_upsert_monthly_records,
    write_source_metadata as repo_write_source_metadata,
)
from .auth import resolve_role, UserRole
from .data_quality import get_quality_monitor
from .forecast import build_forecast
from .forecasting import REQUIRED_COLUMNS, complete_daily_series, load_data
from .gtfs_importer import GTFSValidationError, import_gtfs_feed
from .gtfs_service import (
    get_route_details as gtfs_get_route_details,
    list_agencies as gtfs_list_agencies,
    list_feeds as gtfs_list_feeds,
    list_routes as gtfs_list_routes,
    list_stops as gtfs_list_stops,
)
from .hybrid_forecasting import (
    build_intraday_route_forecast,
    build_network_intraday_curve,
)
from .model_benchmark import run_multi_model_benchmark
from .modeling import weekday_average_forecast
from .monthly_data import MonthlyDataError, MonthlyRidershipStore, MonthlyStoreError
from .occupancy_engine import compute_route_capacity_profile, compute_trip_occupancy
from .operations_engine import get_operations_engine
from .passenger_data import ingest_apc_dataset, parse_and_validate_apc_data
from .realtime import get_realtime_service
from .realtime.provider_registry import get_provider_registry
from .reporting import (
    export_daily_report_csv,
    generate_capacity_risk_report,
    generate_daily_passenger_report,
    send_alert_webhook,
)
from .route_identity import canonicalize_manual_route_id
from .schedule_import import schedules_to_synthetic_counts
from .service_planner import SimulationScenario, run_service_simulation
from .train import train_project


def project_path(relative: str) -> Path:
    """Resolve assets both from source and from a PyInstaller bundle."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parent.parent
    return root / relative


def user_data_dir() -> Path:
    configured = os.getenv("BUS_FORECAST_DATA_DIR")
    if configured:
        p = Path(configured).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    candidates: list[Path] = []
    if sys.platform == "win32":
        candidates.append(Path(os.getenv("LOCALAPPDATA", Path.home())) / "BusDemandForecast")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "BusDemandForecast")
    else:
        candidates.append(Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "bus-demand-forecast")

    workspace_data = project_path("data")
    candidates.append(workspace_data)

    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            test_file = cand / ".probe_write.tmp"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            return cand
        except OSError:
            continue

    workspace_data.mkdir(parents=True, exist_ok=True)
    return workspace_data


DATA_DIR = user_data_dir()
ARTIFACT_DIR = DATA_DIR / "artifacts"
DATA_PATH = DATA_DIR / "daily_ridership.csv"
MONTHLY_DATA_DIR = DATA_DIR / "monthly"
MONTHLY_DATA_PATH = MONTHLY_DATA_DIR / "cta_monthly_ridership.json"
MODEL_PATH = ARTIFACT_DIR / "model.joblib"
METRICS_PATH = ARTIFACT_DIR / "backtest_metrics.json"
PREDICTIONS_PATH = ARTIFACT_DIR / "backtest_predictions.csv"
HOLDOUT_PREDICTIONS_PATH = ARTIFACT_DIR / "holdout_predictions.csv"
FORECAST_PATH = DATA_DIR / "forecast.csv"
FORECAST_META_PATH = DATA_DIR / "forecast_metadata.json"
META_PATH = DATA_DIR / "data_source.json"
AUDIT_PATH = DATA_DIR / "ingest_audit.jsonl"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PIPELINE_LOCK = threading.Lock()
MONTHLY_IMPORT_LOCK = threading.Lock()
PUBLIC_DEMO_LOCK = threading.Lock()
PUBLIC_DEMO_CACHE: dict | None = None
PUBLIC_DEMO_ARTIFACTS: dict[str, Path] = {}

WEB_DIR = project_path("web")
WEB_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

from contextlib import asynccontextmanager
import logging
import shutil

logger = logging.getLogger("bus_demand_app")


def ensure_system_initialized() -> None:
    """Bootstrap demo dataset, trained model, and forecast cache on application startup.

    Guarantees:
    1. If DATA_DIR does not have daily_ridership.csv, imports and dynamically aligns
       the bundled sample_daily_ridership.csv (2,190 records, 3 routes: R10, R22, R35).
    2. If ARTIFACT_DIR does not have model.joblib, copies the bundled pre-trained model
       or trains once reliably, persisting artifacts so requests never retrain synchronously.
    3. Generates 7-day forecast into DATA_DIR / forecast.csv and in-memory PUBLIC_DEMO_CACHE.
    4. Ensures /api/status, /api/public-demo/status, /api/ready, and AI Copilot have instant access
       to verified real/synthetic telemetry on cold start without 30-second delays.
    """
    global PUBLIC_DEMO_CACHE
    sample_csv = project_path("data/sample_daily_ridership.csv")
    bundled_model_dir = project_path("data/bundled_model")
    bundled_artifacts_dir = project_path("data/artifacts")

    # 1. Initialize Database
    try:
        init_db()
    except Exception as exc:
        logger.warning("Database initialization deferred/failed: %s", exc)

    # 2. Bootstrap Dataset in DATA_DIR
    if not DATA_PATH.exists() and sample_csv.exists():
        try:
            raw_data = load_data(str(sample_csv))
            required_cols = {"service_date", "route_id", "passengers"}
            if required_cols.issubset(raw_data.columns) and not raw_data.empty:
                original_dates = pd.DatetimeIndex(sorted(raw_data["service_date"].unique()))
                last_observed_date = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None) - pd.DateOffset(days=1)
                shifted_dates = pd.date_range(end=last_observed_date, periods=len(original_dates), freq="D")
                date_map = dict(zip(original_dates, shifted_dates))
                raw_data["service_date"] = raw_data["service_date"].map(date_map)
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                raw_data.to_csv(DATA_PATH, index=False)
                META_PATH.write_text(
                    json.dumps({
                        "source": "synthetic demo data",
                        "source_system": "bundled_sample",
                        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                    }),
                    encoding="utf-8",
                )
                logger.info("Initialized DATA_PATH from sample dataset: %d records, %d routes.", len(raw_data), raw_data["route_id"].nunique())
        except Exception as exc:
            logger.error("Failed to bootstrap sample dataset: %s", exc)

    # 3. Bootstrap Model Artifacts in ARTIFACT_DIR
    if not MODEL_PATH.exists():
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        candidate_model = None
        candidate_metrics = None
        for cand_dir in [bundled_model_dir, bundled_artifacts_dir]:
            m = cand_dir / "model.joblib"
            met = cand_dir / "metrics.json"
            if m.exists():
                candidate_model = m
                if met.exists():
                    candidate_metrics = met
                break

        if candidate_model:
            try:
                shutil.copy2(candidate_model, MODEL_PATH)
                if candidate_metrics:
                    shutil.copy2(candidate_metrics, METRICS_PATH)
                logger.info("Bootstrapped pre-trained model bundle from %s", candidate_model)
            except Exception as exc:
                logger.warning("Could not copy bundled model: %s", exc)

        if not MODEL_PATH.exists() and DATA_PATH.exists():
            try:
                logger.info("Training initial demo model...")
                train_project(
                    data_path=DATA_PATH,
                    model_out=MODEL_PATH,
                    horizon=7,
                    folds=4,
                    metrics_out=METRICS_PATH,
                    predictions_out=PREDICTIONS_PATH,
                    holdout_predictions_out=HOLDOUT_PREDICTIONS_PATH,
                )
                logger.info("Demo model training finished.")
            except Exception as exc:
                logger.error("Failed to train demo model: %s", exc)

    # 4. Generate & Cache 7-Day Forecast
    if DATA_PATH.exists() and MODEL_PATH.exists():
        try:
            forecasts = build_forecast(DATA_PATH, MODEL_PATH, days=7, missing_days="error")
            forecasts.to_csv(FORECAST_PATH, index=False)
            FORECAST_META_PATH.write_text(
                json.dumps({
                    "mode": "machine_learning",
                    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "horizon_days": 7,
                }),
                encoding="utf-8",
            )
            data = load_data(str(DATA_PATH))
            metrics_dict = {}
            if METRICS_PATH.exists():
                try:
                    metrics_dict = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
                except Exception:
                    pass

            PUBLIC_DEMO_ARTIFACTS.update({"data": DATA_PATH, "model": MODEL_PATH})
            PUBLIC_DEMO_CACHE = {
                "data_loaded": True,
                "source": "synthetic demo data",
                "rows": int(len(data)),
                "route_count": int(data["route_id"].nunique()),
                "latest_observed_date": data["service_date"].max().date().isoformat(),
                "model_trained": True,
                "trained_through": data["service_date"].max().date().isoformat(),
                "horizon_days": 7,
                "metrics": metrics_dict,
                "forecasts": forecasts.to_dict(orient="records"),
                "forecast_updated_at": datetime.now(timezone.utc).isoformat(),
                "public_demo": True,
            }
            logger.info("Initialized PUBLIC_DEMO_CACHE: %d forecast rows for routes %s.", len(forecasts), sorted(data["route_id"].unique().tolist()))
        except Exception as exc:
            logger.error("Failed to generate initial forecast: %s", exc)


@asynccontextmanager
async def lifespan(application: FastAPI):
    try:
        ensure_system_initialized()
    except Exception as exc:
        logger.error("Lifespan startup bootstrap error: %s", exc)
    yield


app = FastAPI(
    title="Bus Passenger Demand Forecast",
    description="Daily route demand forecasting API and installable app.",
    version="0.1.0",
    lifespan=lifespan,
)
cors_origins = [item.strip() for item in os.getenv("BUS_FORECAST_CORS_ORIGINS", "").split(",") if item.strip()]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def authorize(authorization: str | None = Header(default=None)) -> None:
    """Require a configured bearer key on hosted deployments."""
    if os.getenv("BUS_FORECAST_REQUIRE_AUTH", "").lower() not in {"1", "true", "yes"}:
        return
    expected = (
        os.getenv("BUS_FORECAST_API_KEY")
        or os.getenv("BUS_FORECAST_OPERATOR_KEY")
        or os.getenv("BUS_FORECAST_ADMIN_KEY")
    )
    if not expected:
        raise HTTPException(status_code=503, detail="Hosted API authentication is not configured")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Enter the API access key in app settings")


def authorize_ai_chat(authorization: str | None = Header(default=None)) -> UserRole:
    """Authorize AI chat requests, cleanly separating platform access control from OpenRouter keys.

    1. Public Demo Mode: If unauthenticated (no Bearer token provided), public access is allowed
       by default (read-only, rate-limited, grounded in transit telemetry).
    2. Authenticated Operator: Valid Bearer token matching BUS_FORECAST_API_KEY / OPERATOR_KEY.
    3. Authenticated Admin: Valid Bearer token matching BUS_FORECAST_ADMIN_KEY.
    4. Invalid Token: If a token is supplied but incorrect, returns 401 Unauthorized.
    5. Explicit Lockdown: If BUS_FORECAST_REQUIRE_AUTH=true AND BUS_FORECAST_ALLOW_PUBLIC_AI=false,
       unauthenticated requests receive a 401 prompt to enter the operator access key.
    """
    require_auth = os.getenv("BUS_FORECAST_REQUIRE_AUTH", "").lower() in {"1", "true", "yes"}
    allow_public_ai = os.getenv("BUS_FORECAST_ALLOW_PUBLIC_AI", "true").lower() in {"1", "true", "yes"}

    expected_keys: dict[str, UserRole] = {}
    for env_var, role in [
        ("BUS_FORECAST_API_KEY", UserRole.OPERATOR),
        ("BUS_FORECAST_OPERATOR_KEY", UserRole.OPERATOR),
        ("BUS_FORECAST_ADMIN_KEY", UserRole.ADMINISTRATOR),
        ("BUS_FORECAST_ANALYST_KEY", UserRole.ANALYST),
    ]:
        val = os.getenv(env_var)
        if val and val.strip():
            expected_keys[val.strip()] = role

    token = None
    if authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        elif len(parts) == 1:
            token = parts[0]

    # If caller supplied a token, validate it
    if token:
        if expected_keys and token in expected_keys:
            return expected_keys[token]
        if expected_keys:
            raise HTTPException(
                status_code=401,
                detail="Invalid API access key. Enter the correct API access key in app settings.",
            )

    # Unauthenticated caller: check public demo access
    if not require_auth or allow_public_ai:
        return UserRole.PUBLIC

    # Public AI disabled and auth required
    if not expected_keys:
        raise HTTPException(
            status_code=503,
            detail="Hosted API authentication is not configured on the server (BUS_FORECAST_API_KEY missing).",
        )
    raise HTTPException(
        status_code=401,
        detail="Enter the API access key in app settings to use the AI Copilot.",
    )


def require_public_monthly_read() -> None:
    """Keep the narrow public monthly read surface disabled unless explicitly enabled."""
    enabled = os.getenv("BUS_FORECAST_PUBLIC_MONTHLY_READ", "false").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        raise HTTPException(status_code=404, detail="Public monthly route access is disabled")


def protect_pipeline():
    """Serialize uploads, training runs, and forecast writes."""
    if not PIPELINE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A data update or forecasting run is already in progress")
    try:
        yield
    finally:
        PIPELINE_LOCK.release()


def protect_monthly_import():
    """Serialize monthly imports independently from daily data operations."""
    if not MONTHLY_IMPORT_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A monthly data import is already in progress")
    try:
        yield
    finally:
        MONTHLY_IMPORT_LOCK.release()


class TrainRequest(BaseModel):
    horizon: int = Field(default=7, ge=1, le=90)
    folds: int = Field(default=4, ge=1, le=8)
    missing_days: Literal["error", "zero"] = "error"


class ForecastRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=90)
    missing_days: Literal["error", "zero"] = "error"


class DailyCountRecord(BaseModel):
    service_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    route_id: str = Field(min_length=1, max_length=128)
    passengers: float = Field(ge=0)


class DataIngestRequest(BaseModel):
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
    try:
        with get_db_session() as session:
            repo_write_source_metadata(session, metadata, agency_id="default")
    except Exception:
        pass


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
    try:
        with get_db_session() as session:
            repo_record_audit_event(session, action, source_system, record_count, route_count, agency_id="default")
    except Exception:
        pass


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


def persist_daily_data(data: pd.DataFrame, source_system: str = "manual_csv") -> None:
    output = data[["service_date", "route_id", "passengers"]].copy()
    output["service_date"] = pd.to_datetime(output["service_date"]).dt.strftime("%Y-%m-%d")
    temporary = DATA_PATH.with_suffix(".uploading")
    output.to_csv(temporary, index=False)
    temporary.replace(DATA_PATH)
    try:
        with get_db_session() as session:
            records = output.to_dict(orient="records")
            replace_all_daily_counts(session, records, agency_id="default", source_system=source_system)
    except Exception:
        pass


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
    try:
        forecast_metadata = json.loads(FORECAST_META_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        forecast_metadata = {}
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
        "forecast_mode": forecast_metadata.get("mode", "machine_learning" if MODEL_PATH.exists() else "weekday_average" if FORECAST_PATH.exists() else None) if FORECAST_PATH.exists() else None,
        "model_updated_at": pd.Timestamp(MODEL_PATH.stat().st_mtime, unit="s", tz="UTC").isoformat() if MODEL_PATH.exists() else None,
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

    if not result["data_loaded"] and PUBLIC_DEMO_CACHE and PUBLIC_DEMO_CACHE.get("data_loaded"):
        result["data_loaded"] = True
        result["source"] = PUBLIC_DEMO_CACHE.get("source", "synthetic demo data")
        result["rows"] = PUBLIC_DEMO_CACHE.get("rows", 0)
        result["route_count"] = PUBLIC_DEMO_CACHE.get("route_count", 0)
        result["latest_observed_date"] = PUBLIC_DEMO_CACHE.get("latest_observed_date")
        result["model_trained"] = PUBLIC_DEMO_CACHE.get("model_trained", False)
        result["trained_through"] = PUBLIC_DEMO_CACHE.get("trained_through")
        result["horizon_days"] = PUBLIC_DEMO_CACHE.get("horizon_days")
        result["metrics"] = PUBLIC_DEMO_CACHE.get("metrics")
        result["forecasts"] = PUBLIC_DEMO_CACHE.get("forecasts", [])
        result["forecast_mode"] = "machine_learning"
        result["forecast_updated_at"] = PUBLIC_DEMO_CACHE.get("forecast_updated_at")

    try:
        result["db_health"] = check_db_health()
    except Exception:
        result["db_health"] = {"status": "unconfigured"}
    try:
        result["storage_health"] = check_storage_health()
    except Exception:
        result["storage_health"] = None

    return result


def save_forecast(days: int, missing_days: str = "error") -> pd.DataFrame:
    if not DATA_PATH.exists():
        raise HTTPException(status_code=409, detail="Upload daily route counts or load demo data first")
    try:
        if MODEL_PATH.exists():
            forecast = build_forecast(DATA_PATH, MODEL_PATH, days, missing_days)
        else:
            data = load_data(str(DATA_PATH))
            dense = complete_daily_series(data, missing_days=missing_days)
            forecast = weekday_average_forecast(dense, days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    persist_forecast(forecast, "machine_learning" if MODEL_PATH.exists() else "weekday_average")
    return forecast


def save_weekday_baseline(days: int, missing_days: str = "error") -> pd.DataFrame:
    """Save the transparent weekday-average forecast for histories below ML threshold."""
    if not DATA_PATH.exists():
        raise HTTPException(status_code=409, detail="Upload daily route counts first")
    try:
        data = complete_daily_series(load_data(str(DATA_PATH)), missing_days=missing_days)
        forecast = weekday_average_forecast(data, days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    persist_forecast(forecast, "weekday_average")
    return forecast


def persist_forecast(forecast: pd.DataFrame, mode: str) -> None:
    temporary = FORECAST_PATH.with_suffix(".tmp")
    forecast.to_csv(temporary, index=False)
    temporary.replace(FORECAST_PATH)
    metadata_temp = FORECAST_META_PATH.with_suffix(".tmp")
    metadata_temp.write_text(json.dumps({"mode": mode, "updated_at_utc": pd.Timestamp.now(tz="UTC").isoformat()}), encoding="utf-8")
    metadata_temp.replace(FORECAST_META_PATH)


@app.get("/api/health")
@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok", "version": app.version}


@app.get("/api/ready")
@app.get("/ready", include_in_schema=False)
def readiness() -> JSONResponse:
    """Readiness probe checking database connectivity, storage, and initialized forecast services."""
    db_h = check_db_health()
    st_h = check_storage_health()
    data_ready = DATA_PATH.exists() or (PUBLIC_DEMO_CACHE is not None and bool(PUBLIC_DEMO_CACHE.get("data_loaded")))
    model_ready = MODEL_PATH.exists() or (PUBLIC_DEMO_CACHE is not None and bool(PUBLIC_DEMO_CACHE.get("model_trained")))
    forecast_ready = FORECAST_PATH.exists() or (PUBLIC_DEMO_CACHE is not None and bool(PUBLIC_DEMO_CACHE.get("forecasts")))
    routes_count = PUBLIC_DEMO_CACHE.get("route_count", 0) if PUBLIC_DEMO_CACHE else 0
    ready = db_h.get("connected", False) and st_h.get("writable", False) and data_ready and model_ready
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "database": db_h,
            "storage": st_h,
            "data_loaded": bool(data_ready),
            "model_ready": bool(model_ready),
            "forecasts_ready": bool(forecast_ready),
            "routes_available": routes_count,
            "version": app.version,
        },
    )


@app.get("/api/db/health", dependencies=[Depends(authorize)])
def get_db_health() -> dict:
    """Database connectivity and latency inspection."""
    return check_db_health()


@app.get("/api/storage/health", dependencies=[Depends(authorize)])
def get_storage_health() -> dict:
    """Storage directory, write permissions, and disk space."""
    return check_storage_health()


@app.get("/api/db/migrations", dependencies=[Depends(authorize)])
def get_db_migrations() -> dict:
    """Database migration status and table availability."""
    return get_migration_status()


@app.get("/api/status", dependencies=[Depends(authorize)])
def get_status() -> dict:
    return status_payload()


@app.get("/api/monthly-data/status", dependencies=[Depends(authorize)])
def get_monthly_data_status() -> dict:
    """Inspect the separate monthly CTA store without creating or changing it."""
    try:
        return MonthlyRidershipStore(MONTHLY_DATA_PATH).status()
    except MonthlyStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _monthly_route_names(records: list[dict]) -> list[str]:
    """Return distinct non-empty route names in their first-observed order."""
    names = []
    seen = set()
    for record in records:
        name = str(record.get("route_name", "")).strip()
        normalized = name.casefold()
        if name and normalized not in seen:
            names.append(name)
            seen.add(normalized)
    return names


def _month_index(month_start: str) -> int:
    year, month, _day = (int(part) for part in month_start.split("-"))
    return year * 12 + month - 1


def _monthly_route_summary(route_id: str, records: list[dict]) -> dict:
    ordered = sorted(records, key=lambda record: record["month_start"])
    first_month = ordered[0]["month_start"]
    latest = ordered[-1]
    return {
        "route_id": route_id,
        "route_name": next((record["route_name"].strip() for record in reversed(ordered) if record["route_name"].strip()), ""),
        "historical_route_names": _monthly_route_names(ordered),
        "first_month": first_month,
        "last_month": latest["month_start"],
        "observed_months": len(ordered),
        "latest_month": latest["month_start"],
        "latest_boardings": latest["boardings"],
    }


@app.get("/api/monthly-data/routes", dependencies=[Depends(authorize)])
def list_monthly_routes(
    q: str | None = Query(default=None, max_length=100, description="Case-insensitive substring search across route IDs and current or historical names"),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict:
    """Search routes from the separate monthly dataset; never accesses daily data."""
    try:
        records = MonthlyRidershipStore(MONTHLY_DATA_PATH).observations()
    except MonthlyStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not records and os.getenv("DATABASE_URL"):
        try:
            with get_db_session() as session:
                db_records = repo_get_monthly_observations(session, agency_id="cta")
                if db_records:
                    records = db_records
        except Exception:
            pass

    by_route: dict[str, list[dict]] = {}
    for record in records:
        by_route.setdefault(record["route_id"], []).append(record)

    query = (q or "").strip().casefold()
    matching = []
    for route_id, route_records in by_route.items():
        summary = _monthly_route_summary(route_id, route_records)
        searchable = [route_id, *summary["historical_route_names"]]
        if not query or any(query in value.casefold() for value in searchable):
            matching.append(summary)
    matching.sort(key=lambda route: (route["route_id"].casefold(), route["route_id"]))
    return {"query": q, "count": min(len(matching), limit), "total_matches": len(matching), "limit": limit, "routes": matching[:limit]}


@app.get("/api/monthly-data/routes/{route_id}/history", dependencies=[Depends(authorize)])
def get_monthly_route_history(route_id: str) -> dict:
    """Return observed monthly boarding records without filling gaps."""
    normalized_route_id = route_id.strip()
    if not normalized_route_id:
        raise HTTPException(status_code=422, detail="route_id must not be blank")

    try:
        records = [
            record for record in MonthlyRidershipStore(MONTHLY_DATA_PATH).observations()
            if record["route_id"] == normalized_route_id
        ]
    except MonthlyStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not records and os.getenv("DATABASE_URL"):
        try:
            with get_db_session() as session:
                db_res = repo_get_monthly_route_history(session, normalized_route_id, agency_id="cta")
                if db_res:
                    return db_res
        except Exception:
            pass

    if not records:
        raise HTTPException(status_code=404, detail="No monthly history found for this route ID")

    ordered = sorted(records, key=lambda record: record["month_start"])
    summary = _monthly_route_summary(normalized_route_id, ordered)
    month_span = _month_index(summary["last_month"]) - _month_index(summary["first_month"]) + 1
    return {
        "route_id": normalized_route_id,
        "route_name": summary["route_name"],
        "historical_route_names": summary["historical_route_names"],
        "first_month": summary["first_month"],
        "last_month": summary["last_month"],
        "observed_months": summary["observed_months"],
        "missing_months": month_span - summary["observed_months"],
        "boardings_description": "Recorded monthly boardings; these are boardings, not unique passengers.",
        "records": [
            {"month_start": record["month_start"], "route_name": record["route_name"], "boardings": record["boardings"]}
            for record in ordered
        ],
    }


@app.get("/api/public/monthly-data/routes", dependencies=[Depends(require_public_monthly_read)])
def public_monthly_routes(
    q: str | None = Query(default=None, max_length=100, description="Case-insensitive substring search across route IDs and current or historical names"),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict:
    """Public, read-only route discovery when explicitly enabled by deployment config."""
    return list_monthly_routes(q=q, limit=limit)


@app.get("/api/public/monthly-data/routes/{route_id}/history", dependencies=[Depends(require_public_monthly_read)])
def public_monthly_route_history(route_id: str) -> dict:
    """Public, read-only monthly route history when explicitly enabled."""
    return get_monthly_route_history(route_id=route_id)


@app.post(
    "/api/monthly-data/upload",
    dependencies=[Depends(authorize), Depends(protect_monthly_import)],
)
def upload_monthly_data(
    file: UploadFile = File(...),
    source_info: str | None = Form(default=None, max_length=500),
) -> dict:
    """Validate and transactionally upsert a CTA monthly CSV; never trains a model."""
    if not file.filename or Path(file.filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=415, detail="Upload a CSV file")
    contents = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="CSV upload exceeds the 20 MB limit")
    try:
        store = MonthlyRidershipStore(MONTHLY_DATA_PATH)
        imported = store.import_csv(contents, file.filename, source_info)
        status = store.status()
        try:
            with get_db_session() as session:
                obs = store.observations()
                repo_upsert_monthly_records(session, obs, agency_id="cta")
                repo_record_monthly_import_event(session, imported, agency_id="cta")
        except Exception:
            pass
    except MonthlyDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MonthlyStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "message": "Monthly observations imported. No model was trained.",
        "import": imported,
        "status": status,
    }


@app.get("/api/public-demo/status")
def public_demo_status(days: int = Query(default=7, ge=1, le=28)) -> dict:
    """Return a cached, read-only synthetic forecast without touching operator data."""
    if not isinstance(days, int):
        days = getattr(days, "default", 7)
    global PUBLIC_DEMO_CACHE
    if PUBLIC_DEMO_CACHE is not None:
        return public_demo_forecast_for_days(days)
    with PUBLIC_DEMO_LOCK:
        if PUBLIC_DEMO_CACHE is not None:
            return public_demo_forecast_for_days(days)
        ensure_system_initialized()
        if PUBLIC_DEMO_CACHE is not None:
            return public_demo_forecast_for_days(days)
        raise HTTPException(status_code=500, detail="Could not initialize public demo forecast")


def public_demo_forecast_for_days(days: int) -> dict:
    """Return a read-only forecast horizon from the cached synthetic model."""
    if not isinstance(days, int):
        days = getattr(days, "default", 7)
    if PUBLIC_DEMO_CACHE is None:
        raise HTTPException(status_code=503, detail="The synthetic demo model is not ready")
    if days == PUBLIC_DEMO_CACHE["horizon_days"]:
        return PUBLIC_DEMO_CACHE
    data_path = PUBLIC_DEMO_ARTIFACTS.get("data") or DATA_PATH
    model_path = PUBLIC_DEMO_ARTIFACTS.get("model") or MODEL_PATH
    if not data_path.exists() or not model_path.exists():
        raise HTTPException(status_code=503, detail="The synthetic demo model is not ready")
    try:
        forecast = build_forecast(data_path, model_path, days, "error")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Could not generate the synthetic forecast horizon") from exc
    return {**PUBLIC_DEMO_CACHE, "horizon_days": days, "forecasts": forecast.to_dict(orient="records")}
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
    contents = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="CSV upload exceeds the 20 MB limit")
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
        for artifact in (MODEL_PATH, METRICS_PATH, PREDICTIONS_PATH, HOLDOUT_PREDICTIONS_PATH, FORECAST_PATH, FORECAST_META_PATH, ARTIFACT_DIR / "error_analysis.json"):
            artifact.unlink(missing_ok=True)
    source = "synthetic schedule demo data" if imported_schedule else "company upload"
    source_system = "synthetic_schedule_scenario" if imported_schedule else "manual_csv"
    write_metadata(source, source_system, len(data), int(data["route_id"].nunique()))
    record_audit_event("synthetic_schedule_import" if imported_schedule else "replace_upload", source_system, len(data), int(data["route_id"].nunique()))
    FORECAST_PATH.unlink(missing_ok=True)
    FORECAST_META_PATH.unlink(missing_ok=True)
    if imported_schedule:
        return {
            "message": f"Timetable accepted for {int(data['route_id'].nunique())} routes. Generated 730 days of synthetic demonstration counts from schedule frequency; these are NOT measured passenger counts. Train the model to view the illustrative forecast.",
            "status": status_payload(),
        }
    forecast_message = "Data saved. Add at least 28 continuous days per route for a weekday-average forecast."
    if MODEL_PATH.exists():
        try:
            forecast = build_forecast(DATA_PATH, MODEL_PATH, min(7, int(status_payload().get("horizon_days") or 7)), missing_days)
            persist_forecast(forecast, "machine_learning")
            forecast_message = "Data saved and the forecast refreshed using the current model."
        except ValueError as exc:
            FORECAST_PATH.unlink(missing_ok=True)
            FORECAST_META_PATH.unlink(missing_ok=True)
            forecast_message = f"Data saved. Retrain the model before forecasting: {exc}"
    elif data.groupby("route_id")["service_date"].nunique().min() >= 28:
        try:
            save_weekday_baseline(7, missing_days)
            forecast_message = "Data saved and a weekday-average baseline forecast was updated."
        except HTTPException as exc:
            forecast_message = f"Data saved. Baseline forecast is not ready: {exc.detail}"
    return {"message": forecast_message, "status": status_payload()}


@app.post("/api/data/ingest", dependencies=[Depends(authorize), Depends(protect_pipeline)])
def ingest_daily_counts(request: DataIngestRequest) -> dict:
    """Idempotently upsert daily counts from a scheduled company export or integration."""
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
    FORECAST_PATH.unlink(missing_ok=True)
    FORECAST_META_PATH.unlink(missing_ok=True)
    forecast_message = "Counts ingested. Add at least 28 continuous days per route for a weekday-average forecast."
    if MODEL_PATH.exists():
        try:
            horizon = min(90, int(status_payload().get("horizon_days") or 7))
            forecast = build_forecast(DATA_PATH, MODEL_PATH, horizon, request.missing_days)
            persist_forecast(forecast, "machine_learning")
            forecast_message = "Counts ingested and the forecast refreshed using the current model."
        except ValueError as exc:
            FORECAST_PATH.unlink(missing_ok=True)
            FORECAST_META_PATH.unlink(missing_ok=True)
            forecast_message = f"Counts ingested. Retrain the model before forecasting: {exc}"
    elif merged.groupby("route_id")["service_date"].nunique().min() >= 28:
        try:
            save_weekday_baseline(7, request.missing_days)
            forecast_message = "Counts ingested and a weekday-average baseline forecast was updated."
        except HTTPException as exc:
            forecast_message = f"Counts ingested. Baseline forecast is not ready: {exc.detail}"
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
    FORECAST_META_PATH.unlink(missing_ok=True)
    write_metadata("synthetic demo data", "bundled_sample", len(data), int(data["route_id"].nunique()))
    record_audit_event("load_synthetic_demo", "bundled_sample", len(data), int(data["route_id"].nunique()))
    return {"message": "Synthetic demo data loaded. Train it to generate a sample forecast.", "status": status_payload()}


@app.post("/api/train", dependencies=[Depends(authorize), Depends(protect_pipeline)])
def train_model(request: TrainRequest) -> dict:
    if not DATA_PATH.exists():
        raise HTTPException(status_code=409, detail="Upload daily route counts or load demo data first")
    # A 28-day weekday average is useful before the longer history required by ML.
    try:
        data = load_data(str(DATA_PATH))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Saved route data could not be validated: {exc}") from exc
    per_route_days = data.groupby("route_id")["service_date"].nunique()
    holdout_days = max(request.horizon, ((28 + request.horizon - 1) // request.horizon) * request.horizon)
    minimum_ml_days = max(90, 29 + request.horizon * request.folds) + holdout_days
    if per_route_days.empty or int(per_route_days.min()) < minimum_ml_days:
        forecast = save_weekday_baseline(request.horizon, request.missing_days)
        return {
            "message": f"Weekday-average baseline forecast updated. Add at least {minimum_ml_days} continuous days per route to train the machine-learning model for these settings.",
            "mode": "weekday_average",
            "rows": int(len(forecast)),
            "status": status_payload(),
        }
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
        content = "service_date,route_id,predicted_passengers,lower_90_approx,upper_90_approx\n"
        return Response(content=content, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="bus_demand_forecast.csv"', "X-Forecast-Status": "not-ready"})
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


@app.get("/api/template.csv")
def download_template() -> Response:
    return Response(content="service_date,route_id,passengers\n", media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="daily_route_counts_template.csv"'})


@app.post("/api/gtfs/upload", dependencies=[Depends(authorize)])
def upload_gtfs_feed(
    file: UploadFile = File(...),
    feed_id: str | None = Form(default=None),
    agency_id: str | None = Form(default=None),
) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip GTFS archives are accepted")

    content = file.file.read(50 * 1024 * 1024 + 1)
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="GTFS file exceeds maximum size of 50 MB")

    try:
        with get_db_session() as session:
            summary = import_gtfs_feed(
                session=session,
                zip_bytes=content,
                feed_id=feed_id,
                agency_id=agency_id,
            )
            session.commit()
            repo_record_audit_event(
                session,
                action="gtfs_upload",
                source_system="gtfs_zip",
                record_count=summary.routes_count,
                route_count=summary.routes_count,
                agency_id=summary.agency_id,
                details={"feed_id": summary.feed_id, "counts": summary.to_dict()["counts"]},
            )
            session.commit()
            return {"message": "GTFS feed imported successfully", "summary": summary.to_dict()}
    except GTFSValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to process GTFS feed: {exc}")


@app.get("/api/gtfs/feeds")
def get_gtfs_feeds() -> list[dict[str, Any]]:
    try:
        with get_db_session() as session:
            return gtfs_list_feeds(session)
    except Exception:
        return []


@app.get("/api/gtfs/agencies")
def get_gtfs_agencies() -> list[dict[str, Any]]:
    try:
        with get_db_session() as session:
            return gtfs_list_agencies(session)
    except Exception:
        return []


@app.get("/api/gtfs/routes")
def get_gtfs_routes(
    agency_id: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    try:
        clean_agency = agency_id if isinstance(agency_id, str) else None
        clean_search = search if isinstance(search, str) else None
        with get_db_session() as session:
            return gtfs_list_routes(session, agency_id=clean_agency, query=clean_search)
    except Exception:
        return []


@app.get("/api/gtfs/routes/{route_id}")
def get_gtfs_route_details(route_id: str) -> dict[str, Any]:
    try:
        with get_db_session() as session:
            details = gtfs_get_route_details(session, route_id=route_id)
            if not details:
                raise HTTPException(status_code=404, detail=f"Route '{route_id}' not found in transit network")
            return details
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/gtfs/stops")
def get_gtfs_stops(
    search: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    try:
        clean_search = search if isinstance(search, str) else None
        clean_limit = limit if isinstance(limit, int) else 100
        with get_db_session() as session:
            return gtfs_list_stops(session, query=clean_search, limit=clean_limit)
    except Exception:
        return []


@app.get("/api/realtime/vehicles")
def get_realtime_vehicles(
    route_id: str | None = None,
    min_lat: float | None = None,
    max_lat: float | None = None,
    min_lon: float | None = None,
    max_lon: float | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    clean_route = route_id if isinstance(route_id, str) else None
    clean_agency = agency_id if isinstance(agency_id, str) else None
    service = get_realtime_service()
    return service.get_vehicles(
        route_id=clean_route,
        min_lat=float(min_lat) if min_lat is not None and isinstance(min_lat, (int, float, str)) else None,
        max_lat=float(max_lat) if max_lat is not None and isinstance(max_lat, (int, float, str)) else None,
        min_lon=float(min_lon) if min_lon is not None and isinstance(min_lon, (int, float, str)) else None,
        max_lon=float(max_lon) if max_lon is not None and isinstance(max_lon, (int, float, str)) else None,
        agency_id=clean_agency,
    )


@app.get("/api/realtime/vehicles/{vehicle_id}")
def get_realtime_vehicle(vehicle_id: str) -> dict[str, Any]:
    service = get_realtime_service()
    v = service.get_vehicle(vehicle_id)
    if not v:
        raise HTTPException(status_code=404, detail=f"Vehicle '{vehicle_id}' not found in active telemetry")
    return v


@app.get("/api/realtime/trip-updates")
def get_realtime_trip_updates(
    route_id: str | None = None,
    trip_id: str | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    clean_route = route_id if isinstance(route_id, str) else None
    clean_trip = trip_id if isinstance(trip_id, str) else None
    clean_agency = agency_id if isinstance(agency_id, str) else None
    service = get_realtime_service()
    return service.get_trip_updates(route_id=clean_route, trip_id=clean_trip, agency_id=clean_agency)


@app.get("/api/realtime/alerts")
def get_realtime_alerts(
    route_id: str | None = None,
    stop_id: str | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    clean_route = route_id if isinstance(route_id, str) else None
    clean_stop = stop_id if isinstance(stop_id, str) else None
    clean_agency = agency_id if isinstance(agency_id, str) else None
    service = get_realtime_service()
    return service.get_alerts(route_id=clean_route, stop_id=clean_stop, agency_id=clean_agency)


@app.get("/api/realtime/summary")
def get_realtime_summary(agency_id: str | None = None) -> dict[str, Any]:
    clean_agency = agency_id if isinstance(agency_id, str) else None
    service = get_realtime_service()
    return service.get_summary(agency_id=clean_agency)


@app.post("/api/realtime/ingest", dependencies=[Depends(authorize)])
def ingest_realtime_feed(
    file: UploadFile = File(default=None),
    agency_id: str | None = Form(default="default"),
) -> dict[str, Any]:
    if not file:
        raise HTTPException(status_code=400, detail="A GTFS-RT Protobuf or JSON file payload is required")
    raw = file.file.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payload exceeds 20 MB limit")
    try:
        service = get_realtime_service()
        res = service.ingest_payload(raw, agency_id=agency_id or "default")
        return {"message": "GTFS-RT payload ingested successfully", "stats": res}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse GTFS-RT feed: {exc}")


@app.post("/api/realtime/demo-snapshot")
def generate_realtime_demo_snapshot(agency_id: str = "CTA") -> dict[str, Any]:
    """Provide a labeled synthetic live fleet snapshot when no live provider feed is configured."""
    service = get_realtime_service()
    stats = service.generate_synthetic_live_snapshot(agency_id=agency_id)
    return {
        "message": "Synthetic live fleet telemetry snapshot loaded",
        "notice": "All coordinates and delays in this snapshot are synthetic demonstration data.",
        "stats": stats,
    }


@app.get("/api/forecast/realtime/{route_id}")
def get_route_realtime_forecast(
    route_id: str,
    baseline_passengers: float | None = None,
) -> dict[str, Any]:
    base_val = float(baseline_passengers) if baseline_passengers is not None else 2500.0
    forecast = build_intraday_route_forecast(route_id=route_id, daily_baseline_passengers=base_val)
    return forecast.to_dict()


@app.get("/api/forecast/intraday")
def get_intraday_network_curve() -> dict[str, Any]:
    return build_network_intraday_curve()


@app.get("/api/operations/alerts")
def get_operations_alerts(
    route_id: str | None = None,
    severity: str | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    engine = get_operations_engine()
    anomalies, _, _ = engine.evaluate_fleet(agency_id=agency_id)
    filtered = anomalies
    if route_id:
        filtered = [a for a in filtered if a.route_id == route_id]
    if severity:
        filtered = [a for a in filtered if a.severity.upper() == severity.upper()]
    return [a.to_dict() for a in filtered]


@app.get("/api/operations/dispatch-recommendations")
def get_dispatch_recommendations(
    route_id: str | None = None,
    priority: str | None = None,
    agency_id: str | None = None,
) -> list[dict[str, Any]]:
    engine = get_operations_engine()
    _, recs, _ = engine.evaluate_fleet(agency_id=agency_id)
    filtered = recs
    if route_id:
        filtered = [r for r in filtered if r.route_id == route_id]
    if priority:
        filtered = [r for r in filtered if r.priority.upper() == priority.upper()]
    return [r.to_dict() for r in filtered]


@app.get("/api/operations/metrics")
def get_operations_metrics(agency_id: str | None = None) -> dict[str, Any]:
    engine = get_operations_engine()
    _, _, metrics = engine.evaluate_fleet(agency_id=agency_id)
    return metrics.to_dict()


@app.get("/api/quality/drift-status")
def get_quality_drift_status() -> dict[str, Any]:
    monitor = get_quality_monitor()
    df = None
    if DATA_PATH.exists():
        try:
            df = load_data(str(DATA_PATH))
        except Exception:
            pass
    rep = monitor.assess_drift(df)
    return rep.to_dict()


@app.get("/api/quality/audit-logs", dependencies=[Depends(authorize)])
def get_quality_audit_logs(limit: int = 50) -> list[dict[str, Any]]:
    monitor = get_quality_monitor()
    return monitor.get_audit_logs(limit=limit)


@app.post("/api/quality/check-drift", dependencies=[Depends(authorize)])
def check_quality_drift() -> dict[str, Any]:
    monitor = get_quality_monitor()
    df = None
    if DATA_PATH.exists():
        try:
            df = load_data(str(DATA_PATH))
        except Exception:
            pass
    rep = monitor.assess_drift(df)
    return rep.to_dict()


@app.get("/api/auth/roles")
def get_auth_roles(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    role = resolve_role(authorization)
    permissions = {
        UserRole.PUBLIC: ["read_public_demo", "read_gtfs_schedules", "read_health"],
        UserRole.OPERATOR: [
            "read_public_demo",
            "read_gtfs_schedules",
            "read_health",
            "read_forecasts",
            "upload_daily_counts",
            "read_realtime_telemetry",
            "read_operations_board",
        ],
        UserRole.ADMINISTRATOR: [
            "read_public_demo",
            "read_gtfs_schedules",
            "read_health",
            "read_forecasts",
            "upload_daily_counts",
            "read_realtime_telemetry",
            "read_operations_board",
            "train_models",
            "upload_gtfs_schedule",
            "upload_monthly_data",
            "access_quality_drift",
            "manage_database",
        ],
    }
    return {
        "role": role.value,
        "is_authenticated": role != UserRole.PUBLIC,
        "permissions": permissions.get(role, []),
    }


class SimulationRequest(BaseModel):
    route_id: str
    forecast_hourly_demand: float
    baseline_trips_per_hour: float
    modified_trips_per_hour: float
    vehicle_capacity: int = 55
    operating_hours: int = 16


class WebhookNotificationRequest(BaseModel):
    webhook_url: str
    alert_payload: dict[str, Any]


@app.post("/api/passenger-data/upload", dependencies=[Depends(authorize)])
def upload_passenger_data(
    file: UploadFile = File(...),
    agency_id: str = "default",
    source_system: str = "apc",
) -> dict[str, Any]:
    """Ingest standardized APC / AFC passenger counting observations."""
    content = file.file.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds maximum size of 25 MB")

    clean_df, val_res = parse_and_validate_apc_data(content, agency_id=agency_id, source_system=source_system)
    if not val_res.is_valid:
        raise HTTPException(status_code=422, detail={"errors": val_res.error_samples, "validation": val_res.to_dict()})

    with get_db_session() as session:
        result = ingest_apc_dataset(
            session=session,
            df=clean_df,
            agency_id=agency_id,
            source_system=source_system,
            source_filename=file.filename or "upload.csv",
        )
    return {"message": "Passenger data ingested successfully", "validation": val_res.to_dict(), "result": result}


@app.get("/api/occupancy/trip/{trip_id}")
def get_trip_occupancy(
    trip_id: str,
    agency_id: str = "default",
    fallback_capacity: int = 55,
) -> dict[str, Any]:
    """Calculate exact stop-level passenger conservation load for a specific trip."""
    with get_db_session() as session:
        profile = compute_trip_occupancy(
            session=session,
            trip_id=trip_id,
            agency_id=agency_id,
            fallback_capacity=fallback_capacity,
        )
    if not profile:
        raise HTTPException(status_code=404, detail=f"No APC stop-level observations recorded for trip '{trip_id}'")
    return profile.to_dict()


@app.get("/api/occupancy/route/{route_id}")
def get_route_occupancy_profile(
    route_id: str,
    agency_id: str = "default",
) -> dict[str, Any]:
    """Retrieve route-level peak capacity utilization across recent trips."""
    with get_db_session() as session:
        return compute_route_capacity_profile(session=session, route_id=route_id, agency_id=agency_id)


@app.get("/api/model/benchmark")
def get_model_benchmark(
    horizon: int = 7,
    folds: int = 1,
) -> dict[str, Any]:
    """Evaluate multi-model benchmark comparing HistGradientBoosting, RandomForest, and SeasonalNaive."""
    if not DATA_PATH.exists():
        raise HTTPException(status_code=404, detail="No historical passenger dataset available for model benchmarking")
    try:
        data = load_data(str(DATA_PATH))
        return run_multi_model_benchmark(data, horizon=horizon, folds=folds, candidate_models=["hist_gradient_boosting", "random_forest"])
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/simulator/evaluate")
def evaluate_service_simulation(request: SimulationRequest) -> dict[str, Any]:
    """Execute what-if planning simulation comparing planned frequency/capacity against forecast demand."""
    try:
        scenario = SimulationScenario(
            route_id=request.route_id,
            forecast_hourly_demand=request.forecast_hourly_demand,
            baseline_trips_per_hour=request.baseline_trips_per_hour,
            modified_trips_per_hour=request.modified_trips_per_hour,
            vehicle_capacity=request.vehicle_capacity,
            operating_hours=request.operating_hours,
        )
        result = run_service_simulation(scenario)
        return result.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/reports/daily")
def get_daily_report(
    date: str | None = None,
    agency_id: str = "default",
) -> dict[str, Any]:
    """Generate daily passenger demand summary report."""
    target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db_session() as session:
        return generate_daily_passenger_report(session=session, service_date=target_date, agency_id=agency_id)


@app.get("/api/reports/daily/csv")
def download_daily_report_csv(
    date: str | None = None,
    agency_id: str = "default",
) -> Response:
    """Download daily passenger demand summary as CSV."""
    target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db_session() as session:
        rep = generate_daily_passenger_report(session=session, service_date=target_date, agency_id=agency_id)
    csv_content = export_daily_report_csv(rep)
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="daily_passenger_report_{target_date}.csv"'},
    )


@app.get("/api/reports/capacity-risk")
def get_capacity_risk_report(agency_id: str = "default") -> dict[str, Any]:
    """Retrieve operational capacity and overcrowding risk incidents."""
    with get_db_session() as session:
        return generate_capacity_risk_report(session=session, agency_id=agency_id)


@app.post("/api/reports/webhook", dependencies=[Depends(authorize)])
def trigger_alert_webhook(request: WebhookNotificationRequest) -> dict[str, Any]:
    """Dispatch operational alert to a configured webhook receiver."""
    success = send_alert_webhook(request.webhook_url, request.alert_payload)
    if not success:
        raise HTTPException(status_code=502, detail="Failed to deliver alert payload to specified webhook URL")
    return {"status": "DELIVERED", "webhook_url": request.webhook_url}


@app.get("/api/realtime/providers")
def list_realtime_providers() -> list[dict[str, Any]]:
    """List registered GTFS-RT feed providers and connection status."""
    registry = get_provider_registry()
    return registry.list_providers()


@app.post("/api/realtime/providers/poll", dependencies=[Depends(authorize)])
def poll_realtime_provider(agency_id: str = "CTA") -> dict[str, Any]:
    """Trigger manual polling of a registered external GTFS-RT feed provider."""
    registry = get_provider_registry()
    return registry.poll_provider(agency_id)


# ==========================================
# OpenRouter AI Transit Intelligence Assistant
# ==========================================

class AIChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]] | None = None
    route_id: str | None = None


@app.get("/api/ai/status")
def ai_status() -> dict[str, Any]:
    """Get the current configuration status of the AI Assistant."""
    from .ai_assistant import get_ai_status

    return get_ai_status()


@app.post("/api/ai/chat")
async def ai_chat(
    payload: AIChatRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Any:
    """Conversational AI assistant grounded in real transit demand telemetry."""
    # Check platform authorization (supports public demo mode with rate limiting)
    role = authorize_ai_chat(authorization)

    from .ai_assistant import process_chat_message

    client_id = request.client.host if request and request.client else "unknown"
    result = await process_chat_message(
        user_message=payload.message,
        history=payload.history,
        route_id=payload.route_id,
        client_id=f"{role.value}:{client_id}",
        data_dir=DATA_DIR,
    )

    if result.get("status") == "error":
        status_code_map = {
            "API_KEY_MISSING": 503,
            "OPENROUTER_KEY_MISSING": 503,
            "INVALID_API_KEY": 401,
            "CLIENT_RATE_LIMIT": 429,
            "RATE_LIMIT_EXCEEDED": 429,
            "TIMEOUT": 504,
            "EMPTY_MESSAGE": 422,
            "MODEL_UNAVAILABLE": 502,
            "NETWORK_ERROR": 502,
        }
        code = status_code_map.get(result.get("error_code"), 500)
        return JSONResponse(status_code=code, content=result)

    result["role"] = role.value
    return result


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.exception_handler(HTTPException)
async def api_error_handler(_request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


