"""Schemas shared by more than one API domain."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DataLabel(str, Enum):
    """Provenance label every number in this product carries."""

    live = "live"
    historical = "historical"
    forecast = "forecast"
    synthetic = "synthetic"
    imported = "imported"
    stale = "stale"
    unavailable = "unavailable"


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class JobRecord(BaseModel):
    """One background job, as returned by every job endpoint."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    kind: str
    status: JobState
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    submitted_by: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: str | None = None


class IdempotentWriteNotice(BaseModel):
    """Echoed by write endpoints so clients can see replay behavior."""

    idempotency_key: str | None = None
    replayed: bool = False


class DomainError(BaseModel):
    detail: str
