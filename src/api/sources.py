"""Data source management.

Every feed this product can ingest is registered here with its label, expected
cadence, and enablement state. Registration is idempotent so a retried ``POST``
cannot create duplicates.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from .deps import app_module, idempotency_store, require, storage_path, utc_now
from .idempotency import IDEMPOTENCY_HEADER, normalize_key
from .responses import idempotent_json
from .schemas import DataLabel
from .storage import lock_for, read_json, write_json_atomic

router = APIRouter(prefix="/api/sources", tags=["Data source management"])

STORE_NAME = "data_sources.json"
SOURCE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,63}$"


class SourceKind(str, Enum):
    gtfs_static = "gtfs_static"
    gtfs_realtime = "gtfs_realtime"
    observations = "observations"
    weather = "weather"
    monthly = "monthly"


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    kind: SourceKind
    label: DataLabel = DataLabel.imported
    enabled: bool = True
    agency: str | None = Field(default=None, max_length=64)
    expected_cadence_minutes: int | None = Field(default=None, ge=1, le=100_000)
    max_age_minutes: int | None = Field(default=None, ge=1, le=1_000_000)
    notes: str | None = Field(default=None, max_length=500)


class SourceRecord(SourceIn):
    created_at: str
    updated_at: str
    last_success_at: str | None = None


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    label: DataLabel | None = None
    enabled: bool | None = None
    expected_cadence_minutes: int | None = Field(default=None, ge=1, le=100_000)
    max_age_minutes: int | None = Field(default=None, ge=1, le=1_000_000)
    notes: str | None = Field(default=None, max_length=500)
    mark_success: bool = False


def _path():
    return storage_path(STORE_NAME)


def _load() -> dict[str, dict]:
    payload = read_json(_path(), {})
    return payload if isinstance(payload, dict) else {}


def _save(sources: dict[str, dict]) -> None:
    write_json_atomic(_path(), sources)


@router.get("", dependencies=[Depends(require("gtfs:read"))])
def list_sources(kind: SourceKind | None = None, enabled: bool | None = None) -> dict[str, Any]:
    """All registered sources, optionally filtered by kind or enablement."""

    sources = _load()
    items = [
        source
        for source in sources.values()
        if (kind is None or source.get("kind") == kind.value) and (enabled is None or bool(source.get("enabled")) is enabled)
    ]
    items.sort(key=lambda item: (item.get("kind", ""), item.get("source_id", "")))
    return {"sources": items, "count": len(items)}


@router.get("/{source_id}", dependencies=[Depends(require("gtfs:read"))])
def get_source(source_id: str = PathParam(pattern=SOURCE_ID_PATTERN)) -> dict[str, Any]:
    source = _load().get(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Unknown source")
    return {"source": source}


@router.post("", status_code=201, dependencies=[Depends(require("gtfs:write"))])
def upsert_source(
    payload: SourceIn,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    principal=Depends(require("gtfs:write")),
):
    """Register or replace a source. Idempotent on ``Idempotency-Key``."""

    store = idempotency_store()
    key = normalize_key(idempotency_key)

    def produce() -> tuple[int, object]:
        sources = _load()
        existing = sources.get(payload.source_id)
        now = utc_now()
        record = SourceRecord(
            **payload.model_dump(),
            created_at=(existing or {}).get("created_at") or now,
            updated_at=now,
            last_success_at=(existing or {}).get("last_success_at"),
        ).model_dump(mode="json")
        sources[payload.source_id] = record
        _save(sources)
        return 201, {"source": record}

    return idempotent_json(store, "sources.upsert", key, produce, status_code=201)


@router.patch("/{source_id}", dependencies=[Depends(require("gtfs:write"))])
def patch_source(
    payload: SourcePatch,
    source_id: str = PathParam(pattern=SOURCE_ID_PATTERN),
    principal=Depends(require("gtfs:write")),
) -> dict[str, Any]:
    """Enable/disable a source or move its freshness expectations."""

    with lock_for(_path()):
        sources = _load()
        source = sources.get(source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Unknown source")
        changes = payload.model_dump(exclude_none=True, exclude={"mark_success"})
        source.update(changes)
        if payload.mark_success:
            source["last_success_at"] = utc_now()
        source["updated_at"] = utc_now()
        sources[source_id] = source
        _save(sources)
    return {"source": source}


@router.delete("/{source_id}", status_code=204, dependencies=[Depends(require("gtfs:write"))])
def delete_source(source_id: str = PathParam(pattern=SOURCE_ID_PATTERN)) -> None:
    with lock_for(_path()):
        sources = _load()
        if source_id not in sources:
            raise HTTPException(status_code=404, detail="Unknown source")
        sources.pop(source_id)
        _save(sources)


@router.get("/{source_id}/freshness", dependencies=[Depends(require("gtfs:read"))])
def source_freshness(source_id: str = PathParam(pattern=SOURCE_ID_PATTERN)) -> dict[str, Any]:
    """Whether the source is inside its expected freshness window."""

    source = _load().get(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Unknown source")
    last = source.get("last_success_at")
    label = DataLabel.imported.value
    age_minutes: float | None = None
    if last:
        import datetime as dt

        try:
            seen = dt.datetime.fromisoformat(str(last))
            age_minutes = max(0.0, (dt.datetime.now(dt.timezone.utc) - seen).total_seconds() / 60)
        except ValueError:
            age_minutes = None
        limit = source.get("max_age_minutes")
        if age_minutes is None:
            label = DataLabel.unavailable.value
        elif limit and age_minutes > float(limit):
            label = DataLabel.stale.value
        else:
            label = source.get("label", DataLabel.imported.value)
    else:
        label = DataLabel.unavailable.value
    return {"source_id": source_id, "label": label, "age_minutes": age_minutes, "expected": source.get("max_age_minutes")}


_ = app_module  # keeps the lazy accessor importable from this module
