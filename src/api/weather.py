"""Weather integration.

Weather is stored as coarse station observations (no personal data) so demand
models can later explain demand shifts. No provider is hard-coded: a collector
posts normalized observations here, and ``/status`` reports whether a provider
is configured without ever echoing the credential.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .deps import idempotency_store, require, storage_path, utc_now
from .idempotency import IDEMPOTENCY_HEADER, normalize_key
from .pagination import Page, PageParams, page_params
from .responses import idempotent_json
from .storage import append_jsonl, lock_for, read_jsonl

router = APIRouter(prefix="/api/weather", tags=["Weather integration"])

STORE_NAME = "weather_observations.jsonl"
MAX_RECORDS = 20_000
PROVIDER_ENV = "BUS_FORECAST_WEATHER_PROVIDER"
TOKEN_ENV = "BUS_FORECAST_WEATHER_TOKEN"


class WeatherObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    station_id: str = Field(min_length=1, max_length=64)
    observed_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})$")
    temperature_c: float | None = Field(default=None, ge=-90, le=60)
    precipitation_mm: float | None = Field(default=None, ge=0, le=500)
    snow_mm: float | None = Field(default=None, ge=0, le=500)
    wind_kph: float | None = Field(default=None, ge=0, le=400)
    condition: str | None = Field(default=None, max_length=64)


class WeatherBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[WeatherObservation] = Field(min_length=1, max_length=1_000)


class WeatherStoreItem(WeatherObservation):
    recorded_at: str
    agency: str | None = None


def _path():
    return storage_path(STORE_NAME)


def _dedupe_key(row: dict) -> str:
    return f"{row.get('station_id')}|{row.get('observed_at')}"


@router.post("/observations", status_code=201, dependencies=[Depends(require("data:write"))])
def ingest_observations(
    payload: WeatherBatch,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    principal=Depends(require("data:write")),
):
    """Append a batch of observations; identical station/time pairs are skipped."""

    store = idempotency_store()
    key = normalize_key(idempotency_key)

    def produce() -> tuple[int, object]:
        with lock_for(_path()):
            existing = {_dedupe_key(row) for row in read_jsonl(_path())}
            now = utc_now()
            accepted: list[dict] = []
            skipped = 0
            for observation in payload.observations:
                row = observation.model_dump(mode="json")
                if _dedupe_key(row) in existing:
                    skipped += 1
                    continue
                existing.add(_dedupe_key(row))
                row.update({"recorded_at": now, "agency": principal.agency})
                accepted.append(row)
            for row in accepted:
                append_jsonl(_path(), row, max_records=MAX_RECORDS)
        return 201, {"accepted": len(accepted), "skipped": skipped, "recorded_at": now}

    return idempotent_json(store, "weather.ingest", key, produce, status_code=201)


@router.get("/observations", dependencies=[Depends(require("data:read"))])
def list_observations(
    params: PageParams = Depends(page_params),
    station_id: str | None = Query(default=None, max_length=64),
    observed_from: str | None = Query(default=None, max_length=32),
    observed_to: str | None = Query(default=None, max_length=32),
) -> Page[WeatherStoreItem]:
    """Paginated stored observations, newest first."""

    rows = read_jsonl(_path())
    if station_id:
        rows = [row for row in rows if str(row.get("station_id")) == station_id]
    if observed_from:
        rows = [row for row in rows if str(row.get("observed_at", "")) >= observed_from]
    if observed_to:
        rows = [row for row in rows if str(row.get("observed_at", "")) <= observed_to]
    rows.sort(key=lambda row: str(row.get("observed_at", "")), reverse=True)
    items = [
        WeatherStoreItem(
            station_id=row["station_id"],
            observed_at=row["observed_at"],
            temperature_c=row.get("temperature_c"),
            precipitation_mm=row.get("precipitation_mm"),
            snow_mm=row.get("snow_mm"),
            wind_kph=row.get("wind_kph"),
            condition=row.get("condition"),
            recorded_at=str(row.get("recorded_at", "")),
            agency=row.get("agency"),
        )
        for row in rows
        if row.get("station_id") and row.get("observed_at")
    ]
    return Page.build(items, params)


@router.get("/status", dependencies=[Depends(require("data:read"))])
def weather_status() -> dict[str, Any]:
    """Provider configuration and storage summary (never the credential)."""

    rows = read_jsonl(_path())
    stations = sorted({str(row.get("station_id")) for row in rows if row.get("station_id")})
    latest = max((str(row.get("observed_at")) for row in rows if row.get("observed_at")), default=None)
    provider = os.getenv(PROVIDER_ENV, "").strip() or None
    return {
        "provider": provider,
        "configured": bool(provider),
        "token_present": bool(os.getenv(TOKEN_ENV, "").strip()),
        "observations": len(rows),
        "stations": stations[:50],
        "latest_observation": latest,
        "note": "Weather is optional context only. It never changes a published forecast on its own.",
    }


@router.delete("/observations", status_code=204, dependencies=[Depends(require("security:admin"))])
def clear_observations() -> None:
    """Drop the stored observations (administrators only)."""

    with lock_for(_path()):
        target = _path()
        if target.exists():
            target.unlink()
        else:
            raise HTTPException(status_code=404, detail="No observations stored")
