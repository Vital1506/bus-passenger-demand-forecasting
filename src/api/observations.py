"""Passenger observation ingestion.

Only aggregate route/day counts are accepted. Personal passenger identifiers
are refused by the schema (``extra="forbid"`` plus an explicit field screen), so
this endpoint can never become a place where individual travel records land.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import security
from .deps import idempotency_store, require, storage_path, utc_now
from .idempotency import IDEMPOTENCY_HEADER, normalize_key
from .pagination import Page, PageParams, page_params
from .responses import idempotent_json
from .schemas import DataLabel
from .storage import append_jsonl, lock_for, read_jsonl

router = APIRouter(prefix="/api/ingestion/observations", tags=["Passenger observation ingestion"])

STORE_NAME = "observations.jsonl"
MAX_RECORDS = 100_000
MAX_BATCH = 5_000


class ObservationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: str = Field(min_length=1, max_length=128)
    service_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    boardings: int = Field(ge=0, le=10_000_000)
    agency: str | None = Field(default=None, max_length=64)
    label: DataLabel = DataLabel.imported


class ObservationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[ObservationIn] = Field(min_length=1, max_length=MAX_BATCH)


class ObservationRecord(ObservationIn):
    recorded_at: str
    idempotency_scope: str


def _path():
    return storage_path(STORE_NAME)


def _reject_passenger_fields(payload: ObservationBatch) -> None:
    for observation in payload.observations:
        dumped = observation.model_dump()
        for field in dumped:
            if field.lower() in security.PASSENGER_IDENTIFIER_FIELDS:
                raise HTTPException(status_code=422, detail="Passenger identifiers are not accepted")
        if not security.ROUTE_ID_PATTERN.match(observation.route_id):
            raise HTTPException(status_code=422, detail=f"Invalid route_id: {observation.route_id}")


def _key(row: dict) -> str:
    return f"{row.get('route_id')}|{row.get('service_date')}"


@router.post("", status_code=201, dependencies=[Depends(require("data:write"))])
def ingest_observations(
    payload: ObservationBatch,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    principal: security.Principal = Depends(require("data:write")),
):
    """Append aggregate counts. Repeats of a route/day are reported, not duplicated."""

    _reject_passenger_fields(payload)
    store = idempotency_store()
    key = normalize_key(idempotency_key)

    def produce() -> tuple[int, object]:
        with lock_for(_path()):
            existing = {_key(row) for row in read_jsonl(_path())}
            now = utc_now()
            accepted: list[dict] = []
            duplicates: list[dict] = []
            for observation in payload.observations:
                row = observation.model_dump(mode="json")
                row["agency"] = observation.agency or principal.agency
                if _key(row) in existing:
                    duplicates.append({"route_id": row["route_id"], "service_date": row["service_date"]})
                    continue
                existing.add(_key(row))
                row.update({"recorded_at": now, "idempotency_scope": "observations.ingest"})
                accepted.append(row)
            for row in accepted:
                append_jsonl(_path(), row, max_records=MAX_RECORDS)
        return 201, {
            "accepted": len(accepted),
            "duplicates": len(duplicates),
            "duplicate_keys": duplicates[:50],
            "recorded_at": now,
        }

    return idempotent_json(store, "observations.ingest", key, produce, status_code=201)


@router.get("", dependencies=[Depends(require("data:read"))])
def list_observations(
    params: PageParams = Depends(page_params),
    route_id: str | None = Query(default=None, max_length=128),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
) -> Page[ObservationRecord]:
    """Paginated aggregate observations, newest service date first."""

    rows = read_jsonl(_path())
    if route_id:
        rows = [row for row in rows if str(row.get("route_id")) == route_id]
    if date_from:
        rows = [row for row in rows if str(row.get("service_date", "")) >= date_from]
    if date_to:
        rows = [row for row in rows if str(row.get("service_date", "")) <= date_to]
    rows.sort(key=lambda row: (str(row.get("service_date", "")), str(row.get("route_id", ""))), reverse=True)
    items = [
        ObservationRecord(
            route_id=str(row.get("route_id")),
            service_date=str(row.get("service_date")),
            boardings=int(row.get("boardings", 0)),
            agency=row.get("agency"),
            label=row.get("label", DataLabel.imported.value),
            recorded_at=str(row.get("recorded_at", "")),
            idempotency_scope=str(row.get("idempotency_scope", "observations.ingest")),
        )
        for row in rows
        if row.get("route_id") and row.get("service_date")
    ]
    return Page.build(items, params)


@router.get("/summary", dependencies=[Depends(require("data:read"))])
def observation_summary() -> dict[str, Any]:
    rows = read_jsonl(_path())
    routes = {str(row.get("route_id")) for row in rows if row.get("route_id")}
    dates = sorted(str(row.get("service_date")) for row in rows if row.get("service_date"))
    return {
        "records": len(rows),
        "routes": len(routes),
        "first_service_date": dates[0] if dates else None,
        "latest_service_date": dates[-1] if dates else None,
        "total_boardings": sum(int(row.get("boardings", 0)) for row in rows),
    }
