"""Explainable decision-support alerts and service-planning scenarios.

This module is advisory only. It never dispatches vehicles, changes schedules,
or writes operational state. Every scenario result carries explicit assumptions
and ``review_required=True``.

Alert records always contain the same fields so an operator can audit them:
agency, route or trip, alert time, what happened, supporting observations, the
relevant forecast row, the threshold that was crossed, a plain-English
explanation, a suggested action, a data-confidence label, and acknowledgment
status.

Alert types
-----------
- unusually_high_predicted_demand
- route_above_capacity_limit
- unusually_low_demand
- sudden_passenger_count_variation
- missing_boarding_records
- stale_gps_or_weather_feed
- stale_forecasting_model
- significant_increase_in_forecast_error

Scenario types
--------------
- increase_trip_frequency
- add_vehicle
- peak_frequency_change
- low_demand_reduction
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

ALERT_HIGH_DEMAND = "unusually_high_predicted_demand"
ALERT_ABOVE_CAPACITY = "route_above_capacity_limit"
ALERT_LOW_DEMAND = "unusually_low_demand"
ALERT_SUDDEN_VARIATION = "sudden_passenger_count_variation"
ALERT_MISSING_RECORDS = "missing_boarding_records"
ALERT_STALE_FEED = "stale_gps_or_weather_feed"
ALERT_STALE_MODEL = "stale_forecasting_model"
ALERT_FORECAST_ERROR = "significant_increase_in_forecast_error"

# Documented, overridable planning defaults.
DEFAULT_SEATS_PER_VEHICLE = 55.0
DEFAULT_FLEET_SIZE = 4.0
DEFAULT_ROUND_TRIP_MINUTES = 90.0
DEFAULT_PEAK_MINUTES = 240.0
DEFAULT_OFF_PEAK_MINUTES = 840.0
DEFAULT_PEAK_HEADWAY_MINUTES = 12.0
DEFAULT_OFF_PEAK_HEADWAY_MINUTES = 20.0

FORECAST_LOOKAHEAD_DAYS = 7

FEED_STALE_SECONDS = 300.0
WEATHER_STALE_SECONDS = 1800.0
MODEL_STALE_DAYS = 14.0

SUPPORTED_SCENARIO_TYPES = (
    "increase_trip_frequency",
    "add_vehicle",
    "peak_frequency_change",
    "low_demand_reduction",
)

SCENARIO_SCHEMA = {
    "increase_trip_frequency": {
        "required": ["frequency_factor"],
        "description": "Scale all trip frequency by a factor greater than zero.",
    },
    "add_vehicle": {
        "required": ["vehicles_added"],
        "description": "Add one or more vehicles to the route fleet.",
    },
    "peak_frequency_change": {
        "required": ["peak_headway_minutes"],
        "description": "Change the peak-period headway in minutes.",
    },
    "low_demand_reduction": {
        "required": ["reduction_factor"],
        "description": "Reduce off-peak departures by a factor in (0, 1].",
    },
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _number(value, default=None) -> float | None:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def _as_date(value) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def _alert(
    *,
    alert_type: str,
    agency: str,
    route: str,
    what: str,
    supporting_observations: list[str],
    relevant_forecast: dict[str, Any] | None,
    threshold: dict[str, Any],
    explanation: str,
    suggested_action: str,
    data_confidence: str,
) -> dict[str, Any]:
    return {
        "type": alert_type,
        "agency": agency,
        "route_or_trip": route,
        "time": _now(),
        "what": what,
        "supporting_observations": supporting_observations,
        "relevant_forecast": relevant_forecast,
        "threshold": threshold,
        "explanation": explanation,
        "suggested_action": suggested_action,
        "data_confidence": data_confidence,
        "acknowledged": False,
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def explain_alerts(
    observed: pd.DataFrame,
    forecast: pd.DataFrame | None = None,
    *,
    agency: str = "unknown",
    capacity_config: dict[str, Any] | None = None,
    feed_freshness: dict[str, Any] | None = None,
    weather_freshness: dict[str, Any] | None = None,
    model_freshness: dict[str, Any] | None = None,
    window_days: int = 7,
    route_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Return explainable alerts for the supplied observations and forecast.

    ``observed`` needs service_date, route_id, passengers. ``forecast`` needs
    service_date, route_id, predicted_passengers. ``capacity_config`` maps a
    route to seats_per_vehicle / round_trip_minutes / peak_headway_minutes /
    off_peak_headway_minutes. Freshness dicts are optional; when omitted no
    staleness alert is raised.
    """
    if observed is None or observed.empty:
        return []

    frame = observed.copy()
    frame["service_date"] = pd.to_datetime(frame["service_date"]).dt.normalize()
    frame["route_id"] = frame["route_id"].astype(str)
    if route_filter:
        frame = frame[frame["route_id"] == route_filter]
    if frame.empty:
        return []

    forecast_rows = _forecast_index(forecast)
    capacity_map = _capacity_index(capacity_config)
    latest_date = frame["service_date"].max()
    window_start = latest_date - pd.Timedelta(days=max(window_days, 1) - 1)

    alerts: list[dict[str, Any]] = []
    for route in sorted(frame["route_id"].unique()):
        route_frame = frame[frame["route_id"] == route].sort_values("service_date")
        recent = route_frame[route_frame["service_date"] >= window_start]
        alerts.extend(
            _route_alerts(
                agency=agency,
                route=route,
                route_frame=route_frame,
                recent=recent,
                latest_date=latest_date,
                forecast_rows=forecast_rows,
                capacity=capacity_map.get(route),
            )
        )

    alerts.extend(_freshness_alerts(agency, frame, forecast, feed_freshness, weather_freshness, model_freshness))
    alerts.extend(_missing_record_alerts(agency, frame, route_filter))
    return alerts


def _forecast_index(forecast: pd.DataFrame | None) -> dict[tuple[str, pd.Timestamp], dict[str, Any]]:
    if forecast is None or forecast.empty:
        return {}
    frame = forecast.copy()
    if "service_date" not in frame.columns or "route_id" not in frame.columns:
        return {}
    frame["service_date"] = pd.to_datetime(frame["service_date"]).dt.normalize()
    frame["route_id"] = frame["route_id"].astype(str)
    index: dict[tuple[str, pd.Timestamp], dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        index[(row["route_id"], pd.Timestamp(row["service_date"]))] = row
    return index


def _capacity_index(config: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    if not config:
        return {}
    index: dict[str, dict[str, float]] = {}
    for route, values in config.items():
        if not isinstance(values, dict):
            continue
        index[str(route)] = {
            "seats_per_vehicle": _number(values.get("seats_per_vehicle"), DEFAULT_SEATS_PER_VEHICLE),
            "round_trip_minutes": _number(values.get("round_trip_minutes"), DEFAULT_ROUND_TRIP_MINUTES),
            "peak_headway_minutes": _number(values.get("peak_headway_minutes"), DEFAULT_PEAK_HEADWAY_MINUTES),
            "off_peak_headway_minutes": _number(values.get("off_peak_headway_minutes"), DEFAULT_OFF_PEAK_HEADWAY_MINUTES),
        }
    return index


def _route_alerts(
    *,
    agency: str,
    route: str,
    route_frame: pd.DataFrame,
    recent: pd.DataFrame,
    latest_date: pd.Timestamp,
    forecast_rows: dict[tuple[str, pd.Timestamp], dict[str, Any]],
    capacity: dict[str, float] | None,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    history = route_frame["passengers"].to_numpy(dtype=float)
    recent_values = recent["passengers"].to_numpy(dtype=float)

    high_threshold = float(np.percentile(history, 90)) if history.size else float("nan")
    recent_mean = float(np.mean(recent_values)) if recent_values.size else float("nan")
    recent_std = float(np.std(recent_values, ddof=1)) if recent_values.size > 1 else float("nan")

    # One alert per type per route, anchored on the most severe upcoming day so
    # operators are not flooded with one alert per forecast horizon.
    candidates: list[tuple[pd.Timestamp, int, float, dict[str, Any]]] = []
    for (candidate_route, candidate_date), row in forecast_rows.items():
        if candidate_route != route:
            continue
        days_ahead = int((candidate_date - latest_date).days)
        if days_ahead < 1 or days_ahead > FORECAST_LOOKAHEAD_DAYS:
            continue
        predicted = _number(row.get("predicted_passengers"))
        if predicted is None:
            continue
        candidates.append((candidate_date, days_ahead, predicted, row))
    candidates.sort(key=lambda item: item[0])

    anchors = []
    if candidates:
        busiest = max(candidates, key=lambda item: item[2])
        quietest = min(candidates, key=lambda item: item[2])
        anchors = [busiest] if busiest[0] == quietest[0] else [busiest, quietest]

    for target_date, days_ahead, predicted, row in anchors:
        reference = {
            "service_date": target_date.date().isoformat(),
            "route_id": route,
            "predicted_passengers": round(predicted, 2),
            "days_ahead": days_ahead,
        }

        if history.size and np.isfinite(high_threshold) and predicted > high_threshold:
            alerts.append(_alert(
                alert_type=ALERT_HIGH_DEMAND,
                agency=agency,
                route=route,
                what=(
                    f"Forecast demand for route {route} on {target_date.date().isoformat()} "
                    f"is {predicted:.1f} passengers, above the 90th percentile of observed demand."
                ),
                supporting_observations=[
                    f"Observed days used: {history.size}",
                    f"Observed 90th percentile: {high_threshold:.1f}",
                    f"Days ahead: {days_ahead}",
                ],
                relevant_forecast=reference,
                threshold={"method": "observed 90th percentile", "value": round(high_threshold, 2)},
                explanation=(
                    f"Route {route} is forecast above its own recent high-demand band on "
                    f"{target_date.date().isoformat()}. Higher demand than usual is expected, "
                    f"not confirmed."
                ),
                suggested_action=(
                    f"Review vehicle allocation and frequency for route {route} on "
                    f"{target_date.date().isoformat()} before committing any change."
                ),
                data_confidence="medium",
            ))

        if capacity is not None:
            per_day = _capacity_per_day(capacity, target_date.dayofweek >= 5)
            if per_day and predicted > per_day:
                alerts.append(_alert(
                    alert_type=ALERT_ABOVE_CAPACITY,
                    agency=agency,
                    route=route,
                    what=(
                        f"Forecast demand for route {route} on {target_date.date().isoformat()} "
                        f"is {predicted:.1f}, above the configured daily capacity of {per_day:.1f}."
                    ),
                    supporting_observations=[
                        f"Seats per vehicle: {capacity['seats_per_vehicle']:.0f}",
                        f"Round trip: {capacity['round_trip_minutes']:.0f} minutes",
                        f"Peak headway: {capacity['peak_headway_minutes']:.0f} minutes",
                        f"Off-peak headway: {capacity['off_peak_headway_minutes']:.0f} minutes",
                    ],
                    relevant_forecast=reference,
                    threshold={"method": "configured daily capacity", "value": round(per_day, 2)},
                    explanation=(
                        f"Expected demand exceeds the capacity implied by the configured seats and "
                        f"headways. This is a planning comparison, not a measured overload."
                    ),
                    suggested_action=(
                        f"Consider extra vehicles or higher frequency on route {route} for "
                        f"{target_date.date().isoformat()}; an operator must approve any change."
                    ),
                    data_confidence="medium",
                ))

        if np.isfinite(recent_mean) and np.isfinite(recent_std) and recent_std > 0:
            low_threshold = recent_mean - 1.5 * recent_std
            if predicted < low_threshold:
                alerts.append(_alert(
                    alert_type=ALERT_LOW_DEMAND,
                    agency=agency,
                    route=route,
                    what=(
                        f"Forecast demand for route {route} on {target_date.date().isoformat()} "
                        f"is {predicted:.1f}, below the recent low-demand band."
                    ),
                    supporting_observations=[
                        f"Recent mean demand: {recent_mean:.1f}",
                        f"Recent standard deviation: {recent_std:.1f}",
                        f"Low-demand threshold: {low_threshold:.1f}",
                    ],
                    relevant_forecast=reference,
                    threshold={"method": "recent mean minus 1.5 standard deviations", "value": round(low_threshold, 2)},
                    explanation=(
                        f"Demand is forecast below the recent normal range. This can reflect a real "
                        f"drop or a data and feature problem."
                    ),
                    suggested_action=(
                        f"Confirm recent counts and data completeness for route {route} before "
                        f"treating the low forecast as real."
                    ),
                    data_confidence="low",
                ))

    if recent_values.size >= 2 and np.isfinite(recent_std) and recent_std > 0:
        changes = np.abs(np.diff(recent_values))
        largest = float(np.max(changes))
        if largest > 2.0 * recent_std:
            position = int(np.argmax(changes))
            before = float(recent_values[position])
            after = float(recent_values[position + 1])
            alerts.append(_alert(
                alert_type=ALERT_SUDDEN_VARIATION,
                agency=agency,
                route=route,
                what=(
                    f"Route {route} changed by {largest:.1f} passengers between two recent days "
                    f"({before:.1f} to {after:.1f})."
                ),
                supporting_observations=[
                    f"Recent standard deviation: {recent_std:.1f}",
                    f"Largest day-to-day change: {largest:.1f}",
                    f"Threshold: {2.0 * recent_std:.1f}",
                ],
                relevant_forecast=None,
                threshold={"method": "day-to-day change above two standard deviations", "value": round(2.0 * recent_std, 2)},
                explanation=(
                    f"A sharp day-to-day swing was observed on route {route}. It may be a real "
                    f"demand shift or a recording problem."
                ),
                suggested_action=(
                    f"Check boarding records and service notes for route {route} around the change "
                    f"before acting."
                ),
                data_confidence="low",
            ))

    return alerts


def _capacity_per_day(capacity: dict[str, float], is_weekend: bool) -> float | None:
    """Daily seat capacity implied by seats and headways."""
    seats = _number(capacity.get("seats_per_vehicle"))
    peak_headway = _number(capacity.get("peak_headway_minutes"))
    off_peak_headway = _number(capacity.get("off_peak_headway_minutes"))
    if not seats or not peak_headway or not off_peak_headway:
        return None
    if peak_headway <= 0 or off_peak_headway <= 0:
        return None
    if is_weekend:
        departures = DEFAULT_OFF_PEAK_MINUTES / off_peak_headway
    else:
        departures = DEFAULT_PEAK_MINUTES / peak_headway + DEFAULT_OFF_PEAK_MINUTES / off_peak_headway
    return float(seats * departures)


def _missing_record_alerts(
    agency: str,
    frame: pd.DataFrame,
    route_filter: str | None,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if route_filter:
        groups = {route_filter: frame}
    else:
        groups = {str(route): group for route, group in frame.groupby("route_id")}
    for route, group in groups.items():
        dates = pd.DatetimeIndex(sorted(group["service_date"].unique()))
        if len(dates) < 2:
            continue
        expected = pd.date_range(dates.min(), dates.max(), freq="D")
        missing = expected.difference(dates)
        if len(missing) == 0:
            continue
        first_missing = pd.Timestamp(missing[0]).date().isoformat()
        alerts.append(_alert(
            alert_type=ALERT_MISSING_RECORDS,
            agency=agency,
            route=route,
            what=(
                f"Route {route} has {len(missing)} calendar day(s) without boarding records "
                f"between {dates.min().date().isoformat()} and {dates.max().date().isoformat()}."
            ),
            supporting_observations=[
                f"First missing date: {first_missing}",
                f"Observed days: {len(dates)}",
                f"Missing days: {len(missing)}",
            ],
            relevant_forecast=None,
            threshold={"method": "days missing inside the observed range", "value": 1},
            explanation=(
                "Gaps in the observation calendar distort lag and rolling features and make alerts "
                "unreliable until the gap is explained."
            ),
            suggested_action=(
                f"Confirm whether the missing days are zero service or a collection failure for "
                f"route {route}, then correct or document them."
            ),
            data_confidence="low",
        ))
    return alerts


def _freshness_alerts(
    agency: str,
    frame: pd.DataFrame,
    forecast: pd.DataFrame | None,
    feed_freshness: dict[str, Any] | None,
    weather_freshness: dict[str, Any] | None,
    model_freshness: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    stale_reasons: list[str] = []
    feed_seconds = _number((feed_freshness or {}).get("staleness_seconds"))
    if feed_seconds is not None and feed_seconds > FEED_STALE_SECONDS:
        stale_reasons.append(f"GPS/vehicle feed stale by {feed_seconds:.0f} seconds")
    weather_seconds = _number((weather_freshness or {}).get("staleness_seconds"))
    if weather_seconds is not None and weather_seconds > WEATHER_STALE_SECONDS:
        stale_reasons.append(f"weather feed stale by {weather_seconds:.0f} seconds")
    if stale_reasons:
        alerts.append(_alert(
            alert_type=ALERT_STALE_FEED,
            agency=agency,
            route="all",
            what="; ".join(stale_reasons),
            supporting_observations=[
                f"Feed threshold: {FEED_STALE_SECONDS:.0f} seconds",
                f"Weather threshold: {WEATHER_STALE_SECONDS:.0f} seconds",
            ],
            relevant_forecast=None,
            threshold={"method": "configured feed staleness limits", "value": f"feed {FEED_STALE_SECONDS:.0f}s, weather {WEATHER_STALE_SECONDS:.0f}s"},
            explanation=(
                "One or more live sources are older than the configured tolerance, so any live "
                "signal derived from them should be treated as temporarily unreliable."
            ),
            suggested_action="Check the feed pipelines and refresh the stale sources before acting on live signals.",
            data_confidence="low",
        ))

    model_config = model_freshness or {}
    trained_through = _as_date(model_config.get("trained_through"))
    if trained_through is not None:
        threshold_days = _number(model_config.get("threshold_days"), MODEL_STALE_DAYS) or MODEL_STALE_DAYS
        age_days = (dt.date.today() - trained_through).days
        if age_days > threshold_days:
            alerts.append(_alert(
                alert_type=ALERT_STALE_MODEL,
                agency=agency,
                route="all",
                what=(
                    f"Forecasting model was trained through {trained_through.isoformat()}, "
                    f"which is {age_days} days old."
                ),
                supporting_observations=[
                    f"Trained through: {trained_through.isoformat()}",
                    f"Model age: {age_days} days",
                    f"Staleness threshold: {threshold_days:.0f} days",
                ],
                relevant_forecast=None,
                threshold={"method": "configured model freshness", "value": f"{threshold_days:.0f} days"},
                explanation=(
                    "A stale model may have drifted from current demand patterns, so its forecasts "
                    "should be revalidated before operational use."
                ),
                suggested_action="Retrain and revalidate the model with recent observations.",
                data_confidence="medium",
            ))

    alerts.extend(_forecast_error_alerts(agency, forecast))
    return alerts


def _forecast_error_alerts(agency: str, forecast: pd.DataFrame | None) -> list[dict[str, Any]]:
    if forecast is None or forecast.empty:
        return []
    required = {"service_date", "predicted_passengers", "actual_passengers"}
    if not required.issubset(forecast.columns):
        return []
    frame = forecast.copy()
    frame["service_date"] = pd.to_datetime(frame["service_date"]).dt.normalize()
    frame = frame.sort_values("service_date")
    if len(frame) < 6:
        return []
    recent = frame.tail(7)
    prior = frame.iloc[:-7]
    if len(prior) < 3:
        return []
    recent_error = float(np.mean(np.abs(recent["actual_passengers"] - recent["predicted_passengers"])))
    prior_error = float(np.mean(np.abs(prior["actual_passengers"] - prior["predicted_passengers"])))
    if prior_error <= 0 or recent_error <= 1.5 * prior_error:
        return []
    return [_alert(
        alert_type=ALERT_FORECAST_ERROR,
        agency=agency,
        route="all",
        what=(
            f"Recent mean absolute forecast error is {recent_error:.1f}, versus {prior_error:.1f} "
            f"earlier."
        ),
        supporting_observations=[
            f"Recent average absolute error: {recent_error:.1f}",
            f"Earlier average absolute error: {prior_error:.1f}",
            f"Rows compared: {len(recent)} recent / {len(prior)} earlier",
        ],
        relevant_forecast=None,
        threshold={"method": "recent error above 1.5x earlier error", "value": round(1.5 * prior_error, 2)},
        explanation=(
            "Forecast accuracy has deteriorated against realised values, which can follow data "
            "changes, schedule changes, or demand shifts."
        ),
        suggested_action="Review recent inputs and outliers, then retrain or recalibrate before relying on the forecasts.",
        data_confidence="medium",
    )]


# ---------------------------------------------------------------------------
# Service planning scenarios
# ---------------------------------------------------------------------------


def simulate_scenario(
    route: str,
    scenario: dict[str, Any],
    demand_forecast: pd.DataFrame | None = None,
    *,
    agency: str = "unknown",
    capacity_assumptions: dict[str, Any] | None = None,
    window_start: dt.date | None = None,
    window_end: dt.date | None = None,
) -> dict[str, Any]:
    """Estimate capacity against forecast demand for a planning scenario.

    The result is an estimate built only from the supplied assumptions and
    forecast. It is not a guaranteed real-world outcome and never changes
    schedules or dispatches vehicles.
    """
    if not route:
        raise ValueError("route is required")

    scenario_type = scenario.get("type")
    if scenario_type not in SUPPORTED_SCENARIO_TYPES:
        raise ValueError(f"Unsupported scenario type: {scenario_type}")

    assumptions = _planning_assumptions(capacity_assumptions)
    demand = _demand_total(demand_forecast, route, window_start, window_end)
    days = demand["days"] or 1

    base_per_day = _capacity_per_day(assumptions, is_weekend=False)
    scenario_capacity = _scenario_capacity(scenario_type, scenario, assumptions, base_per_day)
    base_total = base_per_day * days if base_per_day is not None else None
    scenario_total = scenario_capacity * days if scenario_capacity is not None else None

    demand_total = demand["total"]
    return {
        "agency": agency,
        "route": route,
        "scenario_type": scenario_type,
        "scenario_request": dict(scenario),
        "assumptions": {key: assumptions[key] for key in (
            "seats_per_vehicle",
            "fleet_size",
            "round_trip_minutes",
            "peak_minutes",
            "off_peak_minutes",
            "peak_headway_minutes",
            "off_peak_headway_minutes",
        )},
        "window": {
            "start": window_start.isoformat() if window_start else None,
            "end": window_end.isoformat() if window_end else None,
            "days": days,
        },
        "demand": {
            "available": demand["available"],
            "total": demand_total,
            "mean_per_day": demand["mean"],
            "rows": demand["rows"],
            "days": demand["days"],
            "note": demand["note"],
        },
        "base_capacity": {
            "per_day": base_per_day,
            "total": base_total,
            "gap_to_demand": (demand_total - base_total) if (demand_total is not None and base_total is not None) else None,
            "status": _capacity_status(demand_total, base_total),
        },
        "scenario_capacity": {
            "per_day": scenario_capacity,
            "total": scenario_total,
            "gap_to_demand": (demand_total - scenario_total) if (demand_total is not None and scenario_total is not None) else None,
            "status": _capacity_status(demand_total, scenario_total),
            "change_in_total": (scenario_total - base_total) if (base_total is not None and scenario_total is not None) else None,
        },
        "planning_note": (
            "Estimate only. Derived from the supplied demand forecast and stated capacity assumptions; "
            "not a guaranteed real-world result and not an authorization to change service."
        ),
        "review_required": True,
        "generated_at": _now(),
    }


def _planning_assumptions(capacity_assumptions: dict[str, Any] | None) -> dict[str, float]:
    source = capacity_assumptions or {}
    return {
        "seats_per_vehicle": _number(source.get("seats_per_vehicle"), DEFAULT_SEATS_PER_VEHICLE),
        "fleet_size": _number(source.get("fleet_size"), DEFAULT_FLEET_SIZE),
        "round_trip_minutes": _number(source.get("round_trip_minutes"), DEFAULT_ROUND_TRIP_MINUTES),
        "peak_minutes": _number(source.get("peak_minutes"), DEFAULT_PEAK_MINUTES),
        "off_peak_minutes": _number(source.get("off_peak_minutes"), DEFAULT_OFF_PEAK_MINUTES),
        "peak_headway_minutes": _number(source.get("peak_headway_minutes"), DEFAULT_PEAK_HEADWAY_MINUTES),
        "off_peak_headway_minutes": _number(source.get("off_peak_headway_minutes"), DEFAULT_OFF_PEAK_HEADWAY_MINUTES),
    }


def _scenario_capacity(
    scenario_type: str,
    scenario: dict[str, Any],
    assumptions: dict[str, float],
    base_per_day: float | None,
) -> float | None:
    if base_per_day is None:
        return None
    seats = assumptions["seats_per_vehicle"]
    peak_minutes = assumptions["peak_minutes"]
    off_peak_minutes = assumptions["off_peak_minutes"]
    peak_headway = assumptions["peak_headway_minutes"]
    off_peak_headway = assumptions["off_peak_headway_minutes"]

    if scenario_type == "increase_trip_frequency":
        factor = _number(scenario.get("frequency_factor"))
        if factor is None or factor <= 0:
            raise ValueError("increase_trip_frequency requires a positive frequency_factor")
        return base_per_day * factor

    if scenario_type == "add_vehicle":
        added = _number(scenario.get("vehicles_added"))
        if added is None or added <= 0:
            raise ValueError("add_vehicle requires a positive vehicles_added")
        fleet = assumptions["fleet_size"]
        if fleet <= 0:
            return base_per_day
        scale = (fleet + added) / fleet
        return base_per_day * scale

    if scenario_type == "peak_frequency_change":
        new_peak_headway = _number(scenario.get("peak_headway_minutes"))
        if new_peak_headway is None or new_peak_headway <= 0:
            raise ValueError("peak_frequency_change requires a positive peak_headway_minutes")
        departures = peak_minutes / new_peak_headway + off_peak_minutes / off_peak_headway
        return float(seats * departures)

    if scenario_type == "low_demand_reduction":
        factor = _number(scenario.get("reduction_factor"))
        if factor is None or not (0 < factor <= 1):
            raise ValueError("low_demand_reduction requires a reduction_factor in (0, 1]")
        off_peak_departures = (off_peak_minutes / off_peak_headway) * factor
        return float(seats * (peak_minutes / peak_headway + off_peak_departures))

    return base_per_day


def _demand_total(
    demand_forecast: pd.DataFrame | None,
    route: str,
    window_start: dt.date | None,
    window_end: dt.date | None,
) -> dict[str, Any]:
    empty = {"available": False, "total": None, "mean": None, "rows": 0, "days": 0,
             "note": "No demand forecast was supplied."}
    if demand_forecast is None or demand_forecast.empty:
        return empty
    if not {"service_date", "route_id", "predicted_passengers"}.issubset(demand_forecast.columns):
        return {**empty, "note": "Forecast is missing service_date, route_id, or predicted_passengers."}

    frame = demand_forecast.copy()
    frame["service_date"] = pd.to_datetime(frame["service_date"]).dt.normalize()
    frame["route_id"] = frame["route_id"].astype(str)
    frame = frame[frame["route_id"] == route]
    if window_start:
        frame = frame[frame["service_date"] >= pd.Timestamp(window_start)]
    if window_end:
        frame = frame[frame["service_date"] <= pd.Timestamp(window_end)]
    if frame.empty:
        return {**empty, "note": f"No demand forecast rows for route {route} in the requested window."}

    values = frame["predicted_passengers"].astype(float)
    return {
        "available": True,
        "total": float(values.sum()),
        "mean": float(values.mean()),
        "rows": int(len(frame)),
        "days": int(frame["service_date"].nunique()),
        "note": "Demand is the supplied forecast summed over the requested window.",
    }


def _capacity_status(demand_total: float | None, capacity_total: float | None) -> str:
    if demand_total is None or capacity_total is None:
        return "unknown"
    return "likely_over_capacity" if demand_total > capacity_total else "within_capacity"
