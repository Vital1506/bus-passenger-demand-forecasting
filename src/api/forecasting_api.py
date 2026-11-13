"""Forecasting jobs.

Training and forecast refresh take seconds to minutes. The existing
``POST /api/train`` and ``POST /api/forecast`` stay for scripted callers; this
domain exposes the same work as pollable jobs so a browser never holds a
request open while scikit-learn fits.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .deps import app_module, require, runner
from .pagination import Page, PageParams, page_params
from .schemas import JobRecord

router = APIRouter(prefix="/api/forecasting", tags=["Forecasting"])

JOB_KINDS = ("train", "forecast")


class ForecastJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["train", "forecast"] = "forecast"
    horizon: int = Field(default=7, ge=1, le=90)
    folds: int = Field(default=4, ge=1, le=8)
    days: int = Field(default=7, ge=1, le=90)
    missing_days: Literal["error", "zero"] = "error"


def _run(payload: ForecastJobRequest) -> dict[str, Any]:
    """Execute inside a worker thread, holding the pipeline lock like the sync API."""

    app = app_module()
    if not app.PIPELINE_LOCK.acquire(blocking=True, timeout=600):
        raise RuntimeError("A data update or forecasting run is already in progress")
    try:
        if payload.kind == "train":
            return app.train_model(
                app.TrainRequest(horizon=payload.horizon, folds=payload.folds, missing_days=payload.missing_days)
            )
        return app.refresh_forecast(app.ForecastRequest(days=payload.days, missing_days=payload.missing_days))
    finally:
        app.PIPELINE_LOCK.release()


@router.post("/jobs", response_model=JobRecord, status_code=202, dependencies=[Depends(require("model:train"))])
def create_job(payload: ForecastJobRequest, principal=Depends(require("model:train"))) -> JobRecord:
    """Queue a training or forecast run and return the job handle."""

    job = runner().submit(
        f"forecast.{payload.kind}",
        _run,
        params={"payload": payload},
        submitted_by=principal.id,
    )
    return JobRecord(**job)


@router.get("/jobs", response_model=Page[JobRecord], dependencies=[Depends(require("forecast:run"))])
def list_jobs(params: PageParams = Depends(page_params)) -> Page[JobRecord]:
    jobs = [job for job in runner().list() if job.get("kind", "").startswith("forecast.")]
    return Page.build([JobRecord(**job) for job in jobs], params)


@router.get("/jobs/{job_id}", response_model=JobRecord, dependencies=[Depends(require("forecast:run"))])
def get_job(job_id: str) -> JobRecord:
    job = runner().get(job_id)
    if not job or not str(job.get("kind", "")).startswith("forecast."):
        raise HTTPException(status_code=404, detail="Unknown forecasting job")
    return JobRecord(**job)


@router.get("/jobs/summary/status", dependencies=[Depends(require("forecast:run"))])
def job_summary() -> dict[str, Any]:
    jobs = [job for job in runner().list() if str(job.get("kind", "")).startswith("forecast.")]
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.get("status", "queued")] = counts.get(job.get("status", "queued"), 0) + 1
    return {"total": len(jobs), "counts": counts}
