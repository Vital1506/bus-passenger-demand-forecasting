"""Reporting.

Reports are aggregate-only exports built from the same modules the existing
``/api/reports`` endpoints use. Generation runs as a background job because a
monthly or capacity report can take seconds; the caller polls the job.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import reports
from .deps import app_module, require, runner, storage_path, utc_now
from .pagination import Page, PageParams, page_params
from .schemas import JobRecord

router = APIRouter(prefix="/api/reporting", tags=["Reporting"])

JOB_KIND = "report.build"


class ReportRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=200)


class ReportRunResult(BaseModel):
    report_id: str
    path: str
    generated_at: str
    rows: int
    bytes: int


@router.get("/catalog", dependencies=[Depends(require("reports:read"))])
def catalog() -> dict[str, Any]:
    """Every defined report with its availability and content notice."""

    app = app_module()
    inputs = app.reports_inputs()
    return app.api_safe({"reports": reports.catalog(**inputs), "notice": reports.NOTICE})


def _build_report(report_id: str) -> dict[str, Any]:
    app = app_module()
    if report_id not in reports.REPORT_IDS:
        raise ValueError(f"Unknown report: {report_id}")
    inputs = app.reports_inputs()
    report = app.api_safe(reports.build_report(report_id, **inputs))
    if not report.get("available"):
        raise ValueError(report.get("unavailable_reason") or "Report unavailable")
    csv_text = reports.report_to_csv(report)
    directory = storage_path("reports")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{report_id}-{utc_now().replace(':', '')}.csv"
    target.write_text(csv_text, encoding="utf-8")
    rows = report.get("rows") or []
    return ReportRunResult(
        report_id=report_id,
        path=str(target),
        generated_at=utc_now(),
        rows=len(rows) if isinstance(rows, list) else 0,
        bytes=len(csv_text.encode("utf-8")),
    ).model_dump(mode="json")


@router.post("/runs", response_model=JobRecord, status_code=202, dependencies=[Depends(require("reports:read"))])
def create_run(payload: ReportRunRequest, principal=Depends(require("reports:read"))) -> JobRecord:
    """Queue a report build. Returns the job to poll instead of blocking."""

    app = app_module()
    if payload.report_id not in reports.REPORT_IDS:
        raise HTTPException(status_code=404, detail="Unknown report")
    job = runner().submit(
        JOB_KIND,
        _build_report,
        params={"report_id": payload.report_id},
        submitted_by=principal.id,
    )
    return JobRecord(**job)


@router.get("/runs", response_model=Page[JobRecord], dependencies=[Depends(require("reports:read"))])
def list_runs(params: PageParams = Depends(page_params)) -> Page[JobRecord]:
    """Paginated report-build history, newest first."""

    jobs = runner().list(kind=JOB_KIND)
    return Page.build([JobRecord(**job) for job in jobs], params)


@router.get("/runs/{job_id}", response_model=JobRecord, dependencies=[Depends(require("reports:read"))])
def get_run(job_id: str) -> JobRecord:
    job = runner().get(job_id)
    if not job or job.get("kind") != JOB_KIND:
        raise HTTPException(status_code=404, detail="Unknown report run")
    return JobRecord(**job)
