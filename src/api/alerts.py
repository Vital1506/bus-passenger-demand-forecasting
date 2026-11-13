"""Alert generation.

Alerts are detected conditions (a route's forecast spiking, data going stale, a
model degrading). Delivery of those alerts lives in :mod:`src.notifications`;
this module owns detection so the two concerns stay separable. Alerts are
aggregate-only and never reference an individual passenger.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from .deps import idempotency_store, require, storage_path, utc_now
from .idempotency import IDEMPOTENCY_HEADER, normalize_key
from .pagination import Page, PageParams, page_params
from .responses import idempotent_json
from .schemas import DataLabel
from .storage import lock_for, read_json, write_json_atomic

router = APIRouter(prefix="/api/alerts", tags=["Alert generation"])

STORE_NAME = "alerts.json"
MAX_ALERTS = 1_000
SEVERITIES = ("info", "warning", "critical")


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: str
    code: str
    severity: str
    title: str
    detail: str
    route_id: str | None = None
    label: DataLabel = DataLabel.forecast
    detected_at: str


class AlertGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demand_spike_percent: float = Field(default=25.0, ge=1, le=500)
    demand_drop_percent: float = Field(default=25.0, ge=1, le=500)
    stale_data_days: int = Field(default=3, ge=1, le=90)


class AlertGenerationResponse(BaseModel):
    evaluated_at: str
    raised: int
    retained: int
    codes: list[str] = Field(default_factory=list)


def _path():
    return storage_path(STORE_NAME)


def _load() -> list[dict]:
    payload = read_json(_path(), [])
    return payload if isinstance(payload, list) else []


def _alert_id(code: str, route_id: str | None) -> str:
    digest = hashlib.sha256(f"{code}|{route_id or ''}".encode("utf-8")).hexdigest()
    return digest[:16]


def _build_alerts(request: AlertGenerationRequest) -> list[Alert]:
    from .deps import app_module

    app = app_module()
    status = app.api_safe(app.status_payload()) or {}
    monitoring = app.api_safe(app.monitoring_payload()) or {}
    now = utc_now()
    detected: list[Alert] = []

    data_age = (status.get("data_age_days") if isinstance(status, dict) else None)
    if isinstance(data_age, (int, float)) and data_age > request.stale_data_days:
        detected.append(
            Alert(
                alert_id=_alert_id("stale-data", None),
                code="stale-data",
                severity="warning",
                title="Passenger data is stale",
                detail=f"The newest observation is {float(data_age):.1f} days old (limit {request.stale_data_days}).",
                label=DataLabel.stale,
                detected_at=now,
            )
        )
    if not status.get("data_loaded", False):
        detected.append(
            Alert(
                alert_id=_alert_id("no-data", None),
                code="no-data",
                severity="info",
                title="No passenger data loaded",
                detail="Upload daily counts or load the synthetic demo before relying on a forecast.",
                label=DataLabel.unavailable,
                detected_at=now,
            )
        )

    for warning in monitoring.get("warnings", []) or []:
        severity = str(warning.get("severity", "info")).lower()
        if severity not in SEVERITIES:
            severity = "info"
        code = f"monitoring:{warning.get('code', 'unknown')}"
        detected.append(
            Alert(
                alert_id=_alert_id(code, warning.get("route_id")),
                code=code,
                severity=severity,
                title=str(warning.get("title", "Monitoring warning"))[:160],
                detail=str(warning.get("detail", ""))[:500],
                route_id=warning.get("route_id"),
                label=DataLabel.historical,
                detected_at=now,
            )
        )

    comparison = monitoring.get("comparison") or {}
    verdict = str(comparison.get("verdict", "")).lower()
    if verdict in {"baseline_better", "degraded"}:
        detected.append(
            Alert(
                alert_id=_alert_id("model-degraded", None),
                code="model-degraded",
                severity="warning",
                title="Forecast accuracy behind the baseline",
                detail="The seasonal-naive baseline is outperforming the trained model on the evaluation window.",
                label=DataLabel.historical,
                detected_at=now,
            )
        )
    return detected


@router.post("/generate", response_model=AlertGenerationResponse, dependencies=[Depends(require("notifications:write"))])
def generate(
    request: AlertGenerationRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    principal=Depends(require("notifications:write")),
):
    """Evaluate thresholds against current status/monitoring and store alerts."""

    body = request or AlertGenerationRequest()
    store = idempotency_store()
    key = normalize_key(idempotency_key)

    def produce() -> tuple[int, object]:
        detected = _build_alerts(body)
        with lock_for(_path()):
            existing = {alert.get("alert_id"): alert for alert in _load()}
            for alert in detected:
                existing[alert.alert_id] = alert.model_dump(mode="json")
            retained = sorted(existing.values(), key=lambda item: str(item.get("detected_at", "")), reverse=True)[:MAX_ALERTS]
            write_json_atomic(_path(), retained)
        response = AlertGenerationResponse(
            evaluated_at=utc_now(),
            raised=len(detected),
            retained=len(retained),
            codes=sorted({alert.code for alert in detected}),
        )
        return 200, response.model_dump(mode="json")

    return idempotent_json(store, "alerts.generate", key, produce)


@router.get("", dependencies=[Depends(require("notifications:read"))])
def list_alerts(
    params: PageParams = Depends(page_params),
    severity: str | None = Query(default=None, max_length=16),
    code: str | None = Query(default=None, max_length=64),
) -> Page[Alert]:
    """Paginated stored alerts, newest first."""

    rows = _load()
    if severity:
        rows = [row for row in rows if str(row.get("severity")) == severity]
    if code:
        rows = [row for row in rows if str(row.get("code")) == code]
    rows.sort(key=lambda row: str(row.get("detected_at", "")), reverse=True)
    items = [Alert(**row) for row in rows if row.get("alert_id") and row.get("code")]
    return Page.build(items, params)


@router.get("/summary", dependencies=[Depends(require("notifications:read"))])
def summary() -> dict[str, Any]:
    rows = _load()
    counts = {severity: 0 for severity in SEVERITIES}
    for row in rows:
        severity = str(row.get("severity", "info"))
        counts[severity] = counts.get(severity, 0) + 1
    return {"total": len(rows), "by_severity": counts, "latest": max((row.get("detected_at") for row in rows), default=None)}
