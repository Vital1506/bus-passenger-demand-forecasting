"""Local and hosted API for the Bus Passenger Demand Forecast app."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .forecast import build_forecast
from .forecasting import complete_daily_series, load_data
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
FORECAST_PATH = DATA_DIR / "forecast.csv"
META_PATH = DATA_DIR / "data_source.json"
AUDIT_PATH = DATA_DIR / "ingest_audit.jsonl"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PIPELINE_LOCK = threading.Lock()
PUBLIC_DEMO_LOCK = threading.Lock()
PUBLIC_DEMO_CACHE: dict | None = None

WEB_DIR = project_path("web")
WEB_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Bus Passenger Demand Forecast",
    description="Daily route demand forecasting API and installable app.",
    version="0.1.0",
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
    expected = os.getenv("BUS_FORECAST_API_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail="Hosted API authentication is not configured")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Enter the API access key in app settings")


def protect_pipeline():
    """Serialize uploads, training runs, and forecast writes."""
    if not PIPELINE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A data update or forecasting run is already in progress")
    try:
        yield
    finally:
        PIPELINE_LOCK.release()


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


@app.get("/api/status", dependencies=[Depends(authorize)])
def get_status() -> dict:
    return status_payload()


@app.get("/api/public-demo/status")
def public_demo_status() -> dict:
    """Return a cached, read-only synthetic forecast without touching operator data."""
    global PUBLIC_DEMO_CACHE
    if PUBLIC_DEMO_CACHE is not None:
        return PUBLIC_DEMO_CACHE
    with PUBLIC_DEMO_LOCK:
        if PUBLIC_DEMO_CACHE is not None:
            return PUBLIC_DEMO_CACHE
        sample = project_path("data/sample_daily_ridership.csv")
        if not sample.exists():
            raise HTTPException(status_code=500, detail="The bundled synthetic dataset is missing")
        demo_dir = Path(tempfile.mkdtemp(prefix="bus-forecast-public-demo-"))
        model_path = demo_dir / "model.joblib"
        metrics_path = demo_dir / "metrics.json"
        predictions_path = demo_dir / "predictions.csv"
        try:
            training = train_project(
                data_path=sample,
                model_out=model_path,
                horizon=7,
                folds=4,
                metrics_out=metrics_path,
                predictions_out=predictions_path,
            )
            data = load_data(str(sample))
            forecasts = build_forecast(sample, model_path, 7, "error")
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
    return PUBLIC_DEMO_CACHE


@app.get("/api/public-demo/forecast.csv")
def public_demo_forecast_csv() -> Response:
    payload = public_demo_status()
    forecast = pd.DataFrame(payload["forecasts"])
    return Response(
        content=forecast.to_csv(index=False),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bus_demand_synthetic_demo_forecast.csv"'},
    )


@app.get("/api/public-demo/sample.csv")
def public_demo_sample_csv() -> Response:
    sample = project_path("data/sample_daily_ridership.csv")
    if not sample.exists():
        raise HTTPException(status_code=500, detail="The bundled synthetic dataset is missing")
    return FileResponse(sample, media_type="text/csv", filename="synthetic_daily_route_counts.csv")


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
    try:
        try:
            data = load_data(str(temporary_path))
            complete_daily_series(data, missing_days=missing_days)
        except (UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        replace_path = DATA_PATH.with_suffix(".uploading")
        replace_path.write_bytes(contents)
        replace_path.replace(DATA_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)
    write_metadata("company upload", "manual_csv", len(data), int(data["route_id"].nunique()))
    record_audit_event("replace_upload", "manual_csv", len(data), int(data["route_id"].nunique()))
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
def download_forecast() -> Response:
    if not FORECAST_PATH.exists():
        raise HTTPException(status_code=404, detail="No forecast has been generated yet")
    return FileResponse(FORECAST_PATH, media_type="text/csv", filename="bus_demand_forecast.csv")


@app.get("/api/template.csv", dependencies=[Depends(authorize)])
def download_template() -> Response:
    return FileResponse(project_path("data/sample_daily_ridership.csv"), media_type="text/csv", filename="daily_route_counts_template.csv")


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
