"""GTFS-Realtime ingestion.

Realtime feeds arrive as repeated snapshots. This module validates the envelope
shape, deduplicates by content checksum, and stores an aggregate snapshot record
(vehicle positions are summarized, never attributed to a passenger).
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .deps import idempotency_store, require, storage_path, utc_now
from .idempotency import IDEMPOTENCY_HEADER, normalize_key
from .pagination import Page, PageParams, page_params
from .responses import idempotent_json
from .schemas import DataLabel
from .storage import append_jsonl, lock_for, read_jsonl

router = APIRouter(prefix="/api/ingestion/gtfs-realtime", tags=["GTFS-Realtime ingestion"])

STORE_NAME = "gtfs_realtime_snapshots.jsonl"
MAX_SNAPSHOTS = 5_000
MAX_ENTITIES = 5_000
SAMPLE_LIMIT = 10


class RealtimeEntity(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=128)


class RealtimeSnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agency: str | None = Field(default=None, max_length=64)
    feed_timestamp: str | None = Field(default=None, max_length=40)
    entity: list[RealtimeEntity] = Field(default_factory=list, max_length=MAX_ENTITIES)


class SnapshotRecord(BaseModel):
    snapshot_id: str
    checksum: str
    agency: str
    received_at: str
    feed_timestamp: str | None
    entity_count: int
    entity_ids: list[str] = Field(default_factory=list)
    label: DataLabel = DataLabel.live


class SnapshotIngestResponse(BaseModel):
    snapshot_id: str | None
    checksum: str
    accepted: bool
    duplicate: bool
    entity_count: int
    received_at: str


def _path():
    return storage_path(STORE_NAME)


@router.post("/snapshots", response_model=SnapshotIngestResponse, status_code=201, dependencies=[Depends(require("gtfs:write"))])
def ingest_snapshot(
    payload: RealtimeSnapshotIn,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    principal=Depends(require("gtfs:write")),
):
    """Store one realtime snapshot. Identical payloads are reported as duplicates."""

    store = idempotency_store()
    key = normalize_key(idempotency_key)
    canonical = payload.model_dump_json(exclude_none=True)
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    agency = payload.agency or principal.agency

    def produce() -> tuple[int, object]:
        with lock_for(_path()):
            existing = {str(row.get("checksum")) for row in read_jsonl(_path())}
            now = utc_now()
            if checksum in existing:
                response = SnapshotIngestResponse(
                    snapshot_id=None,
                    checksum=checksum,
                    accepted=False,
                    duplicate=True,
                    entity_count=len(payload.entity),
                    received_at=now,
                )
                return 200, response.model_dump(mode="json")
            record = SnapshotRecord(
                snapshot_id=checksum[:16],
                checksum=checksum,
                agency=agency,
                received_at=now,
                feed_timestamp=payload.feed_timestamp,
                entity_count=len(payload.entity),
                entity_ids=[entity.id for entity in payload.entity[:SAMPLE_LIMIT]],
            )
            append_jsonl(_path(), record.model_dump(mode="json"), max_records=MAX_SNAPSHOTS)
        response = SnapshotIngestResponse(
            snapshot_id=record.snapshot_id,
            checksum=checksum,
            accepted=True,
            duplicate=False,
            entity_count=record.entity_count,
            received_at=now,
        )
        return 201, response.model_dump(mode="json")

    return idempotent_json(store, "gtfs.realtime.snapshot", key, produce, status_code=201)


@router.get("/snapshots", dependencies=[Depends(require("gtfs:read"))])
def list_snapshots(
    params: PageParams = Depends(page_params),
    agency: str | None = Query(default=None, max_length=64),
) -> Page[SnapshotRecord]:
    rows = read_jsonl(_path())
    if agency:
        rows = [row for row in rows if str(row.get("agency")) == agency]
    rows.sort(key=lambda row: str(row.get("received_at", "")), reverse=True)
    items = [SnapshotRecord(**row) for row in rows if row.get("snapshot_id")]
    return Page.build(items, params)


@router.get("/status", dependencies=[Depends(require("gtfs:read"))])
def status() -> dict[str, Any]:
    rows = read_jsonl(_path())
    latest = rows[-1] if rows else None
    return {
        "snapshots": len(rows),
        "latest": latest,
        "stale": latest is None,
        "label": (DataLabel.live.value if latest else DataLabel.unavailable.value),
        "note": "Realtime snapshots are summarized for occupancy context only; the app never stores trip-level passenger records.",
    }


@router.delete("/snapshots", status_code=204, dependencies=[Depends(require("security:admin"))])
def clear_snapshots() -> None:
    target = _path()
    with lock_for(target):
        if not target.exists():
            raise HTTPException(status_code=404, detail="No snapshots stored")
        target.unlink()
