"""Downloadable operator reports built from aggregate demand data.

Every report is aggregate: route and calendar level boardings only. Individual
passenger, ticket, card, or contact records are never read, never accepted, and
never emitted. ``FORBIDDEN_FIELDS`` is enforced on every row as a guardrail.

Each report can be rendered as CSV (machine readable) or as a standalone
printable HTML document (browser print / save as PDF).
"""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

import numpy as np
import pandas as pd

REPORT_IDS = (
    "daily_demand",
    "weekly_route",
    "monthly_ridership",
    "forecast_accuracy",
    "service_disruption",
    "capacity_risk",
)

REPORT_TITLES = {
    "daily_demand": "Daily demand report",
    "weekly_route": "Weekly route report",
    "monthly_ridership": "Monthly ridership report",
    "forecast_accuracy": "Forecast accuracy report",
    "service_disruption": "Service disruption report",
    "capacity_risk": "Capacity-risk report",
}

REPORT_DESCRIPTIONS = {
    "daily_demand": "Observed and forecast boardings for each service date, with the difference.",
    "weekly_route": "Observed boardings per route per ISO week, beside the forecast for the same days.",
    "monthly_ridership": "Monthly boardings per calendar month, with days and routes observed.",
    "forecast_accuracy": "Model error by route and by forecast lead day, compared with the seasonal-naive baseline.",
    "service_disruption": "Service alerts from an imported GTFS-Realtime feed, if one has been imported.",
    "capacity_risk": "Forecast peak demand against configured route capacity, if capacity assumptions exist.",
}

NOTICE = "Aggregate counts only. No individual passenger, ticket, card, or contact records are included."

FORBIDDEN_FIELDS = {
    "passenger_id",
    "passenger_name",
    "card_number",
    "ticket_id",
    "boarding_id",
    "email",
    "phone",
    "address",
    "date_of_birth",
    "latitude",
    "longitude",
}

DEFAULT_PEAK_MINUTES = 240.0
DEFAULT_OFF_PEAK_MINUTES = 840.0
DEFAULT_SEATS_PER_VEHICLE = 55.0
DEFAULT_PEAK_HEADWAY_MINUTES = 12.0
DEFAULT_OFF_PEAK_HEADWAY_MINUTES = 20.0


CAPACITY_FIELDS = (
    "seats_per_vehicle",
    "round_trip_minutes",
    "peak_headway_minutes",
    "off_peak_headway_minutes",
    "fleet_size",
)


def normalize_capacity_assumptions(payload) -> dict[str, dict[str, float]]:
    """Validate operator capacity assumptions for the capacity-risk report."""
    if payload is None or payload == {}:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("Capacity assumptions must be an object keyed by route")
    normalized: dict[str, dict[str, float]] = {}
    for route, values in payload.items():
        if not isinstance(values, dict):
            raise ValueError(f"Capacity assumptions for route {route} must be an object")
        entry: dict[str, float] = {}
        for key, value in values.items():
            if key not in CAPACITY_FIELDS:
                raise ValueError(f"Unknown capacity field {key} for route {route}")
            number = _number(value)
            if number is None or number <= 0:
                raise ValueError(f"Capacity field {key} for route {route} must be a positive number")
            entry[key] = number
        if entry:
            normalized[str(route)] = entry
    return normalized


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _frame(frame: pd.DataFrame | None, columns: set[str]) -> pd.DataFrame | None:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    if not columns.issubset(frame.columns):
        return None
    subset = frame[list(columns)].copy()
    subset["service_date"] = pd.to_datetime(subset["service_date"], errors="coerce").dt.normalize()
    subset = subset.dropna(subset=["service_date"])
    if "route_id" in subset:
        subset["route_id"] = subset["route_id"].astype(str)
    if "passengers" in subset:
        subset["passengers"] = pd.to_numeric(subset["passengers"], errors="coerce")
        subset = subset.dropna(subset=["passengers"])
    elif "predicted_passengers" in subset:
        subset["predicted_passengers"] = pd.to_numeric(subset["predicted_passengers"], errors="coerce")
        subset = subset.dropna(subset=["predicted_passengers"])
    return subset.reset_index(drop=True) if len(subset) else None


def _columns(*specs: tuple[str, str, str]) -> list[dict[str, str]]:
    return [{"key": key, "label": label, "format": fmt} for key, label, fmt in specs]


def _assert_aggregate(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        leaked = FORBIDDEN_FIELDS.intersection(row)
        if leaked:
            raise ValueError(f"Report rows must not contain individual records: {', '.join(sorted(leaked))}")


def _safe_int(value, default: int = 0) -> int:
    number = _number(value)
    return default if number is None else int(round(number))


def _week_start(value: pd.Timestamp) -> pd.Timestamp:
    return (value - pd.Timedelta(days=value.dayofweek)).normalize()


def _empty_report(report_id: str, reason: str, now: dt.datetime) -> dict[str, Any]:
    return {
        "id": report_id,
        "title": REPORT_TITLES[report_id],
        "description": REPORT_DESCRIPTIONS[report_id],
        "generated_at": now.isoformat(),
        "available": False,
        "unavailable_reason": reason,
        "window": {"start": None, "end": None, "days": 0},
        "columns": [],
        "rows": [],
        "totals": {},
        "notes": [NOTICE],
        "notice": NOTICE,
        "confidentiality": "aggregate_only",
    }


def _report(
    report_id: str,
    *,
    now: dt.datetime,
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    start: str | None,
    end: str | None,
    days: int,
    totals: dict[str, Any],
    notes: list[str],
) -> dict[str, Any]:
    _assert_aggregate(rows)
    return {
        "id": report_id,
        "title": REPORT_TITLES[report_id],
        "description": REPORT_DESCRIPTIONS[report_id],
        "generated_at": now.isoformat(),
        "available": True,
        "unavailable_reason": None,
        "window": {"start": start, "end": end, "days": days},
        "columns": columns,
        "rows": rows,
        "totals": totals,
        "notes": [NOTICE, *notes],
        "notice": NOTICE,
        "confidentiality": "aggregate_only",
    }


# ---------------------------------------------------------------------------
# Individual reports
# ---------------------------------------------------------------------------


def _daily_demand(data: pd.DataFrame | None, forecast: pd.DataFrame | None, now: dt.datetime) -> dict[str, Any]:
    observed = _frame(data, {"service_date", "route_id", "passengers"})
    predicted = _frame(forecast, {"service_date", "route_id", "predicted_passengers"})
    if observed is None:
        return _empty_report("daily_demand", "No daily observations are loaded.", now)

    observed_daily = observed.groupby("service_date").agg(
        observed_boardings=("passengers", "sum"),
        routes_observed=("route_id", "nunique"),
    )
    if predicted is not None:
        forecast_daily = predicted.groupby("service_date")["predicted_passengers"].sum().rename("forecast_boardings")
        combined = observed_daily.join(forecast_daily, how="outer").sort_index()
    else:
        combined = observed_daily.sort_index()

    rows = []
    for date, record in combined.iterrows():
        observed_value = _number(record.get("observed_boardings"))
        forecast_value = _number(record.get("forecast_boardings"))
        difference = (forecast_value - observed_value) if observed_value is not None and forecast_value is not None else None
        difference_percent = (
            100.0 * difference / observed_value if difference is not None and observed_value else None
        )
        rows.append(
            {
                "service_date": date.date().isoformat(),
                "routes_observed": _safe_int(record.get("routes_observed")),
                "observed_boardings": observed_value,
                "forecast_boardings": forecast_value,
                "difference": difference,
                "difference_percent": difference_percent,
            }
        )
    rows.sort(key=lambda item: item["service_date"], reverse=True)
    rows = rows[:92]

    observed_total = float(observed["passengers"].sum())
    forecast_total = _number(combined["forecast_boardings"].sum()) if "forecast_boardings" in combined else None
    return _report(
        "daily_demand",
        now=now,
        columns=_columns(
            ("service_date", "SERVICE DATE", "text"),
            ("routes_observed", "ROUTES OBSERVED", "int"),
            ("observed_boardings", "OBSERVED BOARDINGS", "int"),
            ("forecast_boardings", "FORECAST BOARDINGS", "int"),
            ("difference", "DIFFERENCE (FORECAST - OBSERVED)", "int"),
            ("difference_percent", "DIFFERENCE %", "percent"),
        ),
        rows=rows,
        start=observed["service_date"].min().date().isoformat(),
        end=observed["service_date"].max().date().isoformat(),
        days=int(observed["service_date"].nunique()),
        totals={
            "observed_boardings": observed_total,
            "forecast_boardings": forecast_total,
            "routes_observed": int(observed["route_id"].nunique()),
        },
        notes=[
            "Boardings are daily route totals, not unique passengers.",
            "Forecast rows are shown only for dates the current forecast file covers.",
        ],
    )


def _weekly_route(data: pd.DataFrame | None, forecast: pd.DataFrame | None, now: dt.datetime) -> dict[str, Any]:
    observed = _frame(data, {"service_date", "route_id", "passengers"})
    if observed is None:
        return _empty_report("weekly_route", "No daily observations are loaded.", now)
    observed = observed.assign(week_start=observed["service_date"].map(_week_start))
    observed_weekly = observed.groupby(["week_start", "route_id"])["passengers"].sum().rename("observed_boardings")

    predicted = _frame(forecast, {"service_date", "route_id", "predicted_passengers"})
    if predicted is not None:
        predicted = predicted.assign(week_start=predicted["service_date"].map(_week_start))
        forecast_weekly = predicted.groupby(["week_start", "route_id"])["predicted_passengers"].sum().rename("forecast_boardings")
        combined = observed_weekly.to_frame().join(forecast_weekly, how="outer").sort_index()
    else:
        combined = observed_weekly.to_frame().sort_index()

    rows = []
    for (week, route), record in combined.iterrows():
        observed_value = _number(record.get("observed_boardings"))
        forecast_value = _number(record.get("forecast_boardings"))
        difference = (forecast_value - observed_value) if observed_value is not None and forecast_value is not None else None
        wape = (
            abs(difference) / observed_value * 100.0 if difference is not None and observed_value else None
        )
        rows.append(
            {
                "week_start": week.date().isoformat(),
                "route": str(route),
                "observed_boardings": observed_value,
                "forecast_boardings": forecast_value,
                "difference": difference,
                "difference_percent": wape,
            }
        )
    rows.sort(key=lambda item: (item["week_start"], item["route"]), reverse=True)
    rows = rows[:400]

    return _report(
        "weekly_route",
        now=now,
        columns=_columns(
            ("week_start", "WEEK START (MON)", "text"),
            ("route", "ROUTE", "text"),
            ("observed_boardings", "OBSERVED BOARDINGS", "int"),
            ("forecast_boardings", "FORECAST BOARDINGS", "int"),
            ("difference", "DIFFERENCE", "int"),
            ("difference_percent", "ABSOLUTE DIFFERENCE %", "percent"),
        ),
        rows=rows,
        start=observed["service_date"].min().date().isoformat(),
        end=observed["service_date"].max().date().isoformat(),
        days=int(observed["service_date"].nunique()),
        totals={
            "weeks": int(observed["week_start"].nunique()),
            "routes": int(observed["route_id"].nunique()),
            "observed_boardings": float(observed["passengers"].sum()),
        },
        notes=[
            "Weeks start on Monday; partial weeks at the edges of the data are included as observed.",
            "Difference % is the absolute difference divided by observed boardings for that route and week.",
        ],
    )


def _monthly_ridership(data: pd.DataFrame | None, now: dt.datetime) -> dict[str, Any]:
    observed = _frame(data, {"service_date", "route_id", "passengers"})
    if observed is None:
        return _empty_report("monthly_ridership", "No daily observations are loaded.", now)
    observed = observed.assign(month=observed["service_date"].dt.to_period("M").dt.to_timestamp())
    monthly = observed.groupby("month").agg(
        observed_boardings=("passengers", "sum"),
        days_observed=("service_date", "nunique"),
        routes_observed=("route_id", "nunique"),
    )
    rows = []
    for month, record in monthly.sort_index(ascending=False).iterrows():
        days = int(record["days_observed"])
        total = float(record["observed_boardings"])
        rows.append(
            {
                "month": month.strftime("%Y-%m"),
                "observed_boardings": total,
                "days_observed": days,
                "routes_observed": int(record["routes_observed"]),
                "average_per_day": total / days if days else None,
            }
        )
    rows = rows[:36]
    return _report(
        "monthly_ridership",
        now=now,
        columns=_columns(
            ("month", "MONTH", "text"),
            ("observed_boardings", "OBSERVED BOARDINGS", "int"),
            ("days_observed", "DAYS OBSERVED", "int"),
            ("routes_observed", "ROUTES OBSERVED", "int"),
            ("average_per_day", "AVERAGE PER OBSERVED DAY", "int"),
        ),
        rows=rows,
        start=observed["service_date"].min().date().isoformat(),
        end=observed["service_date"].max().date().isoformat(),
        days=int(observed["service_date"].nunique()),
        totals={
            "months": int(observed["month"].nunique()),
            "observed_boardings": float(observed["passengers"].sum()),
        },
        notes=[
            "Monthly totals sum daily route boardings; they are not farebox revenue or unique riders.",
            "Months are reported only for dates present in the daily store.",
        ],
    )


def _forecast_accuracy(metrics: dict | None, now: dt.datetime) -> dict[str, Any]:
    if not isinstance(metrics, dict) or not metrics:
        return _empty_report("forecast_accuracy", "No backtest metrics are available.", now)
    holdout = metrics.get("final_holdout") if isinstance(metrics.get("final_holdout"), dict) else {}
    by_route = holdout.get("by_route") if isinstance(holdout.get("by_route"), dict) and holdout.get("by_route") else metrics.get("by_route")
    rows: list[dict[str, Any]] = []
    if isinstance(by_route, dict):
        for route in sorted(by_route, key=lambda item: str(item)):
            scores = by_route[route] if isinstance(by_route[route], dict) else {}
            model = scores.get("model") or {}
            baseline = scores.get("seasonal_naive") or {}
            model_wape = _number(model.get("wape"))
            baseline_wape = _number(baseline.get("wape"))
            rows.append(
                {
                    "route": str(route),
                    "model_mae": _number(model.get("mae")),
                    "model_rmse": _number(model.get("rmse")),
                    "model_wape_percent": model_wape * 100.0 if model_wape is not None else None,
                    "baseline_wape_percent": baseline_wape * 100.0 if baseline_wape is not None else None,
                    "better": "Model" if (model_wape is not None and baseline_wape is not None and model_wape <= baseline_wape) else "Baseline",
                }
            )
    if not rows:
        model = metrics.get("model") or {}
        baseline = metrics.get("seasonal_naive") or {}
        if _number(model.get("wape")) is None:
            return _empty_report("forecast_accuracy", "No per-route or overall backtest scores are available.", now)
        model_wape = _number(model.get("wape"))
        baseline_wape = _number(baseline.get("wape"))
        rows.append(
            {
                "route": "all routes",
                "model_mae": _number(model.get("mae")),
                "model_rmse": _number(model.get("rmse")),
                "model_wape_percent": model_wape * 100.0 if model_wape is not None else None,
                "baseline_wape_percent": baseline_wape * 100.0 if baseline_wape is not None else None,
                "better": "Model" if (model_wape is not None and baseline_wape is not None and model_wape <= baseline_wape) else "Baseline",
            }
        )

    horizon_rows = []
    error_analysis = holdout.get("error_analysis") if isinstance(holdout.get("error_analysis"), dict) else {}
    for lead, scores in (error_analysis.get("by_lead_day") or {}).items():
        block = scores if isinstance(scores, dict) else {}
        wape = _number(block.get("wape"))
        horizon_rows.append(
            {
                "route": f"Day {lead}",
                "model_mae": _number(block.get("mae")),
                "model_rmse": _number(block.get("rmse")),
                "model_wape_percent": wape * 100.0 if wape is not None else None,
                "baseline_wape_percent": None,
                "better": "—",
            }
        )
    horizon_rows.sort(key=lambda item: int(item["route"].split()[-1]) if item["route"].split()[-1].isdigit() else 0)

    period = holdout if holdout else metrics.get("model_selection_period", {})
    return _report(
        "forecast_accuracy",
        now=now,
        columns=_columns(
            ("route", "ROUTE / HORIZON", "text"),
            ("model_mae", "MODEL MAE", "float"),
            ("model_rmse", "MODEL RMSE", "float"),
            ("model_wape_percent", "MODEL WAPE", "percent"),
            ("baseline_wape_percent", "BASELINE WAPE", "percent"),
            ("better", "BETTER", "text"),
        ),
        rows=[*rows, *horizon_rows],
        start=str(period.get("start")) if period.get("start") else None,
        end=str(period.get("end")) if period.get("end") else None,
        days=int(period.get("days") or 0),
        totals={
            "routes": len(rows),
            "holdout_available": bool(holdout),
            "scored_rows": len(rows) + len(horizon_rows),
        },
        notes=[
            "WAPE is total absolute error divided by total observed boardings for the same rows.",
            "The evaluation period is the time-based backtest, not the live forecast window.",
            "Horizon rows carry the model scores from the untouched holdout; baseline columns are not split by horizon here.",
        ],
    )


def _service_disruption(disruptions: list[dict[str, Any]] | None, now: dt.datetime) -> dict[str, Any]:
    if not disruptions:
        return _empty_report(
            "service_disruption",
            "No GTFS-Realtime service alerts have been imported, so there is nothing to report.",
            now,
        )
    rows = []
    for record in disruptions:
        if not isinstance(record, dict):
            continue
        rows.append(
            {
                "start": str(record.get("start") or record.get("start_time") or record.get("service_date") or "—"),
                "agency": str(record.get("agency") or record.get("agency_code") or "—"),
                "route": str(record.get("route") or record.get("route_id") or "all"),
                "effect": str(record.get("effect") or "UNKNOWN"),
                "cause": str(record.get("cause") or "UNKNOWN"),
                "summary": str(record.get("header") or record.get("summary") or "")[:300],
            }
        )
    if not rows:
        return _empty_report("service_disruption", "The disruption feed returned no usable records.", now)
    rows.sort(key=lambda item: item["start"], reverse=True)
    rows = rows[:500]
    return _report(
        "service_disruption",
        now=now,
        columns=_columns(
            ("start", "START", "text"),
            ("agency", "AGENCY", "text"),
            ("route", "ROUTE", "text"),
            ("effect", "EFFECT", "text"),
            ("cause", "CAUSE", "text"),
            ("summary", "SUMMARY", "text"),
        ),
        rows=rows,
        start=rows[-1]["start"][:10] if rows[-1]["start"] else None,
        end=rows[0]["start"][:10] if rows[0]["start"] else None,
        days=0,
        totals={"alerts": len(rows), "routes": len({row["route"] for row in rows})},
        notes=[
            "Records come from the imported GTFS-Realtime service-alert feed only; no disruption is inferred.",
            "Effects and causes use the GTFS-Realtime vocabulary reported by the source agency.",
        ],
    )


def _daily_capacity(assumptions: dict[str, Any], is_weekend: bool = False) -> float | None:
    seats = _number(assumptions.get("seats_per_vehicle")) or DEFAULT_SEATS_PER_VEHICLE
    peak_headway = _number(assumptions.get("peak_headway_minutes")) or DEFAULT_PEAK_HEADWAY_MINUTES
    off_peak_headway = _number(assumptions.get("off_peak_headway_minutes")) or DEFAULT_OFF_PEAK_HEADWAY_MINUTES
    if peak_headway <= 0 or off_peak_headway <= 0 or seats <= 0:
        return None
    departures = DEFAULT_OFF_PEAK_MINUTES / off_peak_headway
    if not is_weekend:
        departures += DEFAULT_PEAK_MINUTES / peak_headway
    return float(seats * departures)


def _capacity_risk(
    forecast: pd.DataFrame | None,
    capacity: dict[str, Any] | None,
    now: dt.datetime,
) -> dict[str, Any]:
    predicted = _frame(forecast, {"service_date", "route_id", "predicted_passengers"})
    if predicted is None:
        return _empty_report("capacity_risk", "No forecast is available to compare with capacity.", now)
    if not isinstance(capacity, dict) or not capacity:
        return _empty_report(
            "capacity_risk",
            "No route capacity assumptions are configured, so capacity risk cannot be assessed.",
            now,
        )
    rows = []
    for route in sorted(predicted["route_id"].unique()):
        assumptions = capacity.get(route)
        if not isinstance(assumptions, dict):
            continue
        route_rows = predicted[predicted["route_id"] == route]
        peak = route_rows.loc[route_rows["predicted_passengers"].idxmax()]
        peak_value = float(peak["predicted_passengers"])
        peak_date = pd.Timestamp(peak["service_date"])
        daily = _daily_capacity(assumptions, is_weekend=peak_date.dayofweek >= 5)
        if not daily:
            continue
        utilization = 100.0 * peak_value / daily
        risk = "critical" if utilization >= 100.0 else "warning" if utilization >= 90.0 else "within_capacity"
        rows.append(
            {
                "route": route,
                "peak_date": peak_date.date().isoformat(),
                "forecast_peak": peak_value,
                "daily_capacity": daily,
                "utilization_percent": utilization,
                "gap": peak_value - daily,
                "risk": risk,
            }
        )
    if not rows:
        return _empty_report(
            "capacity_risk",
            "Forecast routes do not match the configured capacity assumptions.",
            now,
        )
    rows.sort(key=lambda item: item["utilization_percent"], reverse=True)
    return _report(
        "capacity_risk",
        now=now,
        columns=_columns(
            ("route", "ROUTE", "text"),
            ("peak_date", "PEAK FORECAST DATE", "text"),
            ("forecast_peak", "FORECAST PEAK BOARDINGS", "int"),
            ("daily_capacity", "DAILY SEAT CAPACITY", "int"),
            ("utilization_percent", "UTILIZATION", "percent"),
            ("gap", "GAP (PEAK - CAPACITY)", "int"),
            ("risk", "RISK SIGNAL", "text"),
        ),
        rows=rows,
        start=predicted["service_date"].min().date().isoformat(),
        end=predicted["service_date"].max().date().isoformat(),
        days=int(predicted["service_date"].nunique()),
        totals={
            "routes": len(rows),
            "at_or_over_capacity": sum(1 for row in rows if row["risk"] == "critical"),
            "near_capacity": sum(1 for row in rows if row["risk"] == "warning"),
        },
        notes=[
            "Capacity is seats per day implied by configured seats and headways, not a measured vehicle load.",
            "Risk signals compare expected boardings with that estimate; an operator must review any planning change.",
        ],
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build_report(
    report_id: str,
    *,
    data: pd.DataFrame | None = None,
    forecast: pd.DataFrame | None = None,
    metrics: dict | None = None,
    disruptions: list[dict[str, Any]] | None = None,
    capacity: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Build one report. Unknown ids raise; missing inputs return an unavailable report."""
    if report_id not in REPORT_IDS:
        raise ValueError(f"Unknown report: {report_id}")
    timestamp = now or dt.datetime.now(dt.timezone.utc)
    if report_id == "daily_demand":
        return _daily_demand(data, forecast, timestamp)
    if report_id == "weekly_route":
        return _weekly_route(data, forecast, timestamp)
    if report_id == "monthly_ridership":
        return _monthly_ridership(data, timestamp)
    if report_id == "forecast_accuracy":
        return _forecast_accuracy(metrics, timestamp)
    if report_id == "service_disruption":
        return _service_disruption(disruptions, timestamp)
    return _capacity_risk(forecast, capacity, timestamp)


def catalog(
    *,
    data: pd.DataFrame | None = None,
    forecast: pd.DataFrame | None = None,
    metrics: dict | None = None,
    disruptions: list[dict[str, Any]] | None = None,
    capacity: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Availability of every report for the current data."""
    items = []
    for report_id in REPORT_IDS:
        report = build_report(
            report_id,
            data=data,
            forecast=forecast,
            metrics=metrics,
            disruptions=disruptions,
            capacity=capacity,
            now=now,
        )
        items.append(
            {
                "id": report_id,
                "title": report["title"],
                "description": report["description"],
                "available": report["available"],
                "unavailable_reason": report["unavailable_reason"],
                "rows": len(report["rows"]),
            }
        )
    return items


def _format_value(value: Any, fmt: str) -> str:
    if value is None or value == "":
        return "—"
    if fmt == "int":
        number = _number(value)
        return "—" if number is None else f"{round(number):,}"
    if fmt == "float":
        number = _number(value)
        return "—" if number is None else f"{number:,.2f}"
    if fmt == "percent":
        number = _number(value)
        return "—" if number is None else f"{number:,.1f}%"
    return str(value)


def report_to_csv(report: dict[str, Any]) -> str:
    """Render the report as CSV with human labels and raw numeric values."""
    columns = report.get("columns") or []
    if not columns:
        return ""
    lines = [",".join(_csv_cell(column["label"]) for column in columns)]
    for row in report.get("rows") or []:
        cells = []
        for column in columns:
            value = row.get(column["key"])
            if isinstance(value, float):
                cells.append(f"{value:.6g}" if np.isfinite(value) else "")
            else:
                cells.append(_csv_cell("" if value is None else str(value)))
        lines.append(",".join(cells))
    return "\r\n".join(lines) + "\r\n"


def _csv_cell(value: str) -> str:
    text = str(value)
    if any(character in text for character in ',"\n\r'):
        return '"' + text.replace('"', '""') + '"'
    return text


def report_to_html(report: dict[str, Any]) -> str:
    """Render a self-contained, printable HTML document for one report."""
    title = html.escape(str(report.get("title") or "Report"))
    rows = report.get("rows") or []
    columns = report.get("columns") or []
    generated = html.escape(str(report.get("generated_at") or ""))
    window = report.get("window") or {}
    period = (
        f"{html.escape(str(window.get('start') or '—'))} to {html.escape(str(window.get('end') or '—'))}"
        if window.get("start") or window.get("end")
        else "—"
    )
    if not report.get("available"):
        body = (
            f"<section class='unavailable'><h2>Report unavailable</h2>"
            f"<p>{html.escape(str(report.get('unavailable_reason') or 'Supporting data is missing.'))}</p></section>"
        )
    else:
        head = "".join(f"<th>{html.escape(column['label'])}</th>" for column in columns)
        body_rows = "".join(
            "<tr>" + "".join(
                f"<td class='{'num' if column['format'] in {'int', 'float', 'percent'} else 'txt'}'>"
                f"{html.escape(_format_value(row.get(column['key']), column['format']))}</td>"
                for column in columns
            ) + "</tr>"
            for row in rows
        ) or f"<tr><td colspan='{max(len(columns), 1)}'>No rows for this period.</td></tr>"
        totals = report.get("totals") or {}
        totals_markup = "".join(
            "<td><span>"
            + html.escape(str(key).replace("_", " ").title())
            + "</span><strong>"
            + html.escape(_format_value(value, "float" if isinstance(value, float) else "int"))
            + "</strong></td>"
            for key, value in totals.items()
            if isinstance(value, (int, float))
        )
        body = (
            f"<table class='totals'><tbody><tr>{totals_markup}</tr></tbody></table>"
            f"<table class='data'><thead><tr>{head}</tr></thead><tbody>{body_rows}</tbody></table>"
        )
    notes = "".join(f"<li>{html.escape(str(note))}</li>" for note in report.get("notes") or [])
    css = """
  :root{color-scheme:light}
  body{margin:0;padding:28px;background:#f6f8f7;color:#16363b;font:13px/1.5 "Segoe UI",Arial,sans-serif}
  header{border-bottom:2px solid #087e83;padding-bottom:12px;margin-bottom:16px}
  h1{margin:0 0 4px;font-size:22px;letter-spacing:-.02em}
  .meta{color:#526b70;font-size:11px;font-family:ui-monospace,monospace}
  .notice{margin:12px 0 16px;padding:10px 12px;border:1px solid #cfe3dc;border-radius:8px;background:#eef7f3;font-size:11px}
  table{width:100%;border-collapse:collapse;background:#fff}
  .totals{margin:0 0 14px;border:1px solid #dce8e5;border-radius:8px;overflow:hidden}
  .totals td{padding:8px 10px;border-right:1px solid #e8efec;font-size:10px;color:#526b70;vertical-align:top}
  .totals td:last-child{border-right:0}
  .totals span{display:block;text-transform:uppercase;letter-spacing:.06em;font-size:9px}
  .totals strong{display:block;font-size:15px;color:#16363b;margin-top:2px}
  .data th{background:#eef4f2;text-align:left;font-size:10px;letter-spacing:.04em;padding:8px 10px;border-bottom:1px solid #dce8e5}
  .data td{padding:7px 10px;border-bottom:1px solid #eef2f1;font-size:11px}
  .data tbody tr:nth-child(even){background:#fbfdfc}
  .num{text-align:right;font-variant-numeric:tabular-nums}
  .txt{text-align:left}
  .notes{margin-top:16px;color:#526b70;font-size:10.5px;padding-left:16px}
  .unavailable{padding:22px;border:1px solid #eec4b6;border-radius:10px;background:#fdf4f0}
  footer{margin-top:20px;padding-top:10px;border-top:1px solid #dce8e5;color:#7c8f91;font-size:10px}
  @media print{
    body{background:#fff;padding:0;font-size:11px}
    .no-print{display:none!important}
    .data tr{page-break-inside:avoid}
    .data thead{display:table-header-group}
  }
"""
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\" />\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
        f"<title>{title}</title>\n<style>{css}</style></head>\n<body>\n"
        "<header>\n"
        f"  <h1>{title}</h1>\n"
        f"  <p class=\"meta\">Generated {generated} · Period {period} · Confidentiality: aggregate only</p>\n"
        "</header>\n"
        f"<p class=\"notice\">{html.escape(NOTICE)}</p>\n"
        f"{body}\n"
        f"<ul class=\"notes\">{notes}</ul>\n"
        "<footer>Bus Demand Forecast · planning support only. Review estimates with operations before acting.</footer>\n"
        "</body></html>"
    )
