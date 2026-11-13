"""Static GTFS ingestion.

Validation is cheap and synchronous; the database import is queued as a job so a
large feed cannot block the event loop. Uploads are screened by
:mod:`src.security` before any byte is parsed.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .. import gtfs_static, security
from .deps import app_module, require, runner
from .pagination import Page, PageParams, page_params
from .schemas import JobRecord

router = APIRouter(prefix="/api/ingestion/gtfs-static", tags=["GTFS static ingestion"])

JOB_KIND = "gtfs.static.import"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class FeedSummary(BaseModel):
    file_name: str
    checksum: str
    agencies: int = 0
    routes: int = 0
    stops: int = 0
    trips: int = 0
    stop_times: int = 0
    service_days: int = 0


class FeedImportResult(BaseModel):
    file_name: str
    checksum: str
    feed_id: int | None = None
    imported: dict[str, Any] = Field(default_factory=dict)


def _read_upload(upload: UploadFile) -> bytes:
    contents = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Feed is larger than the 200 MB import limit")
    try:
        security.safe_upload_name(upload.filename or "feed.zip")
    except security.InputValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return contents


def _summarize(parsed: dict) -> FeedSummary:
    def count(key: str) -> int:
        value = parsed.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return len(value)
        return int(value or 0)

    agencies = parsed.get("agencies") or parsed.get("agency") or []
    return FeedSummary(
        file_name="",
        checksum="",
        agencies=len(agencies) if isinstance(agencies, list) else 1,
        routes=count("routes"),
        stops=count("stops"),
        trips=count("trips"),
        stop_times=count("stop_times"),
        service_days=count("service_days") or count("calendar"),
    )


@router.post("/validate", response_model=FeedSummary, dependencies=[Depends(require("gtfs:write"))])
def validate(file: UploadFile = File(...)) -> FeedSummary:
    """Parse a feed and report its contents without touching the database."""

    contents = _read_upload(file)
    try:
        parsed = gtfs_static.parse_feed(contents)
    except gtfs_static.GtfsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    summary = _summarize(parsed)
    summary.file_name = file.filename or "feed.zip"
    summary.checksum = hashlib.sha256(contents).hexdigest()
    return summary


def _import_feed(contents: bytes, file_name: str, checksum: str) -> dict[str, Any]:
    from .. import db

    with db.session_scope() as session:
        result = gtfs_static.import_feed(session, contents, file_name, checksum)
    return FeedImportResult(file_name=file_name, checksum=checksum, feed_id=result.get("feed_id"), imported=result).model_dump(mode="json")


@router.post("/import", response_model=JobRecord, status_code=202, dependencies=[Depends(require("gtfs:write"))])
def import_feed(file: UploadFile = File(...), principal=Depends(require("gtfs:write"))) -> JobRecord:
    """Queue a GTFS import. Poll the returned job for progress."""

    contents = _read_upload(file)
    checksum = hashlib.sha256(contents).hexdigest()
    file_name = file.filename or "feed.zip"
    job = runner().submit(
        JOB_KIND,
        _import_feed,
        params={"contents": contents, "file_name": file_name, "checksum": checksum},
        submitted_by=principal.id,
    )
    return JobRecord(**job)


@router.get("/jobs", response_model=Page[JobRecord], dependencies=[Depends(require("gtfs:read"))])
def list_jobs(params: PageParams = Depends(page_params)) -> Page[JobRecord]:
    jobs = runner().list(kind=JOB_KIND)
    return Page.build([JobRecord(**job) for job in jobs], params)


@router.get("/jobs/{job_id}", response_model=JobRecord, dependencies=[Depends(require("gtfs:read"))])
def get_job(job_id: str) -> JobRecord:
    job = runner().get(job_id)
    if not job or job.get("kind") != JOB_KIND:
        raise HTTPException(status_code=404, detail="Unknown GTFS import job")
    return JobRecord(**job)


@router.get("/status", dependencies=[Depends(require("gtfs:read"))])
def status() -> dict[str, Any]:
    """Whether the GTFS database is reachable and what the last import did."""

    from .. import db

    try:
        health = db.check_database()
    except Exception as exc:  # noqa: BLE001 - report, never crash the status call
        health = {"connected": False, "error": str(exc)[:200]}
    jobs = runner().list(kind=JOB_KIND)
    latest = jobs[0] if jobs else None
    return {
        "database": health,
        "imports": len(jobs),
        "latest_import": latest,
        "agency": app_module().security_registry().get("workspace_agency", "default"),
    }
