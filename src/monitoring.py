"""Model monitoring and data-quality reporting for the daily forecast.

Everything here is read-only and advisory. The module never trains a model and
never edits operational data: retraining is only ever recommended, and the
recommendation lists the checks an operator must satisfy first.

A snapshot answers: which model is deployed, when it was trained, what data it
covered, how fresh the observations and forecasts are, how accurate it has been
overall / by route / by horizon compared with the seasonal-naive baseline, how
much data is missing, whether the demand distribution has moved, and whether
anything suggests the model has degraded.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_POLICY: dict[str, float] = {
    "max_model_age_days": 14.0,
    "max_data_age_days": 2.0,
    "max_forecast_age_days": 7.0,
    "max_missing_percent": 5.0,
    "max_wape_gap": 0.0,
    "max_holdout_degradation": 0.20,
    "distribution_shift_percent": 30.0,
    "distribution_window_days": 14.0,
    "min_observations_per_route": 118.0,
}

POLICY_BOUNDS = {
    "max_model_age_days": (1.0, 365.0),
    "max_data_age_days": (0.0, 90.0),
    "max_forecast_age_days": (0.0, 90.0),
    "max_missing_percent": (0.0, 100.0),
    "max_wape_gap": (0.0, 1.0),
    "max_holdout_degradation": (0.0, 10.0),
    "distribution_shift_percent": (1.0, 500.0),
    "distribution_window_days": (2.0, 90.0),
    "min_observations_per_route": (7.0, 3650.0),
}

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def normalize_policy(overrides: dict[str, Any] | None = None) -> dict[str, float]:
    """Return a complete policy, rejecting unknown keys and invalid bounds."""
    policy = dict(DEFAULT_POLICY)
    if not overrides:
        return policy
    for key, value in overrides.items():
        if key not in DEFAULT_POLICY:
            raise ValueError(f"Unknown monitoring policy field: {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Monitoring policy field {key} must be a number")
        numeric = float(value)
        if not np.isfinite(numeric):
            raise ValueError(f"Monitoring policy field {key} must be finite")
        low, high = POLICY_BOUNDS[key]
        if numeric < low or numeric > high:
            raise ValueError(f"Monitoring policy field {key} must be between {low:g} and {high:g}")
        policy[key] = numeric
    return policy


def load_policy(path: str | Path) -> dict[str, float]:
    """Read a saved policy, falling back to defaults when it is absent or invalid."""
    file_path = Path(path)
    if not file_path.exists():
        return dict(DEFAULT_POLICY)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_POLICY)
    return normalize_policy(payload if isinstance(payload, dict) else None)


def save_policy(path: str | Path, overrides: dict[str, Any]) -> dict[str, float]:
    """Validate and persist a policy. Returns the merged policy."""
    policy = normalize_policy(overrides)
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(file_path)
    return policy


# ---------------------------------------------------------------------------
# Small coercion helpers
# ---------------------------------------------------------------------------


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _iso_day(value) -> str | None:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError):
        return None


def _age_days(value, now: dt.datetime) -> float | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return round(float((pd.Timestamp(now) - stamp) / pd.Timedelta(days=1)), 2)


def _metric_block(value) -> dict[str, float | None]:
    block = value if isinstance(value, dict) else {}
    return {name: _number(block.get(name)) for name in ("mae", "rmse", "wape")}


def _clean_frame(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    if not {"service_date", "route_id", "passengers"}.issubset(frame.columns):
        return None
    cleaned = frame[["service_date", "route_id", "passengers"]].copy()
    cleaned["service_date"] = pd.to_datetime(cleaned["service_date"], errors="coerce").dt.normalize()
    cleaned["route_id"] = cleaned["route_id"].astype(str)
    cleaned["passengers"] = pd.to_numeric(cleaned["passengers"], errors="coerce")
    cleaned = cleaned.dropna(subset=["service_date", "passengers"])
    cleaned = cleaned.drop_duplicates(subset=["service_date", "route_id"], keep="last")
    return cleaned.sort_values(["route_id", "service_date"]).reset_index(drop=True) if len(cleaned) else None


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _model_section(bundle: dict | None, now: dt.datetime) -> dict[str, Any]:
    payload = bundle if isinstance(bundle, dict) else {}
    trained_at = payload.get("created_at_utc")
    return {
        "available": bool(payload),
        "version": payload.get("model_version"),
        "trained_at": trained_at,
        "trained_through": payload.get("trained_through"),
        "horizon_days": int(payload["horizon"]) if _number(payload.get("horizon")) else None,
        "routes": list(payload.get("routes") or []),
        "route_count": len(payload.get("routes") or []),
        "feature_count": len(payload.get("features") or []),
        "data_sha256": payload.get("data_sha256"),
        "interval_method": payload.get("interval_method"),
        "package_versions": payload.get("package_versions") or {},
        "age_days": _age_days(trained_at, now),
    }


def _data_section(data: pd.DataFrame | None, now: dt.datetime) -> dict[str, Any]:
    if data is None:
        return {
            "available": False,
            "rows": 0,
            "route_count": 0,
            "first_date": None,
            "last_observed_date": None,
            "span_days": 0,
            "expected_route_days": 0,
            "missing_route_days": 0,
            "missing_percent": None,
            "min_observations_per_route": None,
            "routes_with_gaps": [],
            "age_days": None,
        }
    dates = pd.DatetimeIndex(sorted(data["service_date"].unique()))
    routes = sorted(data["route_id"].unique())
    span_days = int((dates.max() - dates.min()).days) + 1
    expected = span_days * len(routes)
    observed = int(len(data))
    missing = max(expected - observed, 0)
    per_route = data.groupby("route_id")["service_date"].nunique()
    gaps = []
    for route in routes:
        observed_days = int(per_route.get(route, 0))
        if observed_days < span_days:
            gaps.append({"route": route, "observed_days": observed_days, "missing_days": span_days - observed_days})
    return {
        "available": True,
        "rows": observed,
        "route_count": len(routes),
        "first_date": dates.min().date().isoformat(),
        "last_observed_date": dates.max().date().isoformat(),
        "span_days": span_days,
        "expected_route_days": expected,
        "missing_route_days": missing,
        "missing_percent": round(100.0 * missing / expected, 2) if expected else None,
        "min_observations_per_route": int(per_route.min()) if len(per_route) else None,
        "routes_with_gaps": gaps[:20],
        "age_days": _age_days(dates.max(), now),
    }


def _forecast_section(forecast: pd.DataFrame | None, forecast_updated_at, now: dt.datetime) -> dict[str, Any]:
    if forecast is None or "service_date" not in forecast.columns:
        return {
            "available": False,
            "updated_at": forecast_updated_at,
            "first_date": None,
            "last_date": None,
            "horizon_days": 0,
            "rows": 0,
            "age_days": _age_days(forecast_updated_at, now),
        }
    dates = pd.to_datetime(forecast["service_date"], errors="coerce").dropna().dt.normalize()
    if dates.empty:
        return {
            "available": False,
            "updated_at": forecast_updated_at,
            "first_date": None,
            "last_date": None,
            "horizon_days": 0,
            "rows": int(len(forecast)),
            "age_days": _age_days(forecast_updated_at, now),
        }
    return {
        "available": True,
        "updated_at": forecast_updated_at,
        "first_date": dates.min().date().isoformat(),
        "last_date": dates.max().date().isoformat(),
        "horizon_days": int(dates.nunique()),
        "rows": int(len(forecast)),
        "age_days": _age_days(forecast_updated_at, now),
    }


def _metrics_section(metrics: dict | None) -> dict[str, Any]:
    payload = metrics if isinstance(metrics, dict) else {}
    holdout = payload.get("final_holdout") if isinstance(payload.get("final_holdout"), dict) else {}
    model = _metric_block(payload.get("model"))
    baseline = _metric_block(payload.get("seasonal_naive"))
    holdout_model = _metric_block(holdout.get("model")) if holdout else {name: None for name in ("mae", "rmse", "wape")}
    holdout_baseline = _metric_block(holdout.get("seasonal_naive")) if holdout else {name: None for name in ("mae", "rmse", "wape")}

    by_route_source = holdout.get("by_route") if isinstance(holdout.get("by_route"), dict) and holdout.get("by_route") else payload.get("by_route")
    by_route = []
    if isinstance(by_route_source, dict):
        for route in sorted(by_route_source, key=lambda item: str(item)):
            scores = by_route_source[route] if isinstance(by_route_source[route], dict) else {}
            model_scores = _metric_block(scores.get("model"))
            baseline_scores = _metric_block(scores.get("seasonal_naive"))
            gap = None
            better = None
            if model_scores["wape"] is not None and baseline_scores["wape"] is not None:
                gap = round(model_scores["wape"] - baseline_scores["wape"], 4)
                better = model_scores["wape"] <= baseline_scores["wape"]
            by_route.append(
                {
                    "route": str(route),
                    "model": model_scores,
                    "baseline": baseline_scores,
                    "wape_gap": gap,
                    "model_better": better,
                }
            )

    horizon_source = {}
    error_analysis = holdout.get("error_analysis") if isinstance(holdout.get("error_analysis"), dict) else {}
    if isinstance(error_analysis.get("by_lead_day"), dict):
        horizon_source = error_analysis["by_lead_day"]
    by_horizon = []
    for lead in sorted(horizon_source, key=lambda item: int(item) if str(item).lstrip("-").isdigit() else 0):
        scores = _metric_block(horizon_source[lead])
        by_horizon.append({"horizon_days": int(lead) if str(lead).lstrip("-").isdigit() else lead, **scores})

    return {
        "available": bool(payload),
        "source": "final_holdout" if by_route and holdout.get("by_route") else ("model_selection" if by_route else None),
        "selection": {"model": model, "baseline": baseline},
        "holdout": {
            "available": bool(holdout),
            "days": holdout.get("days"),
            "start": holdout.get("start"),
            "end": holdout.get("end"),
            "model": holdout_model,
            "baseline": holdout_baseline,
        },
        "backtest_folds": payload.get("backtest_folds"),
        "horizon_days": payload.get("horizon_days"),
        "evaluation_period": payload.get("model_selection_period"),
        "by_route": by_route,
        "by_horizon": by_horizon,
    }


def _baseline_comparison(metrics_section: dict[str, Any]) -> dict[str, Any]:
    holdout = metrics_section["holdout"]
    model_wape = holdout["model"]["wape"] if holdout["available"] else metrics_section["selection"]["model"]["wape"]
    baseline_wape = holdout["baseline"]["wape"] if holdout["available"] else metrics_section["selection"]["baseline"]["wape"]
    if model_wape is None or baseline_wape is None:
        return {"model_wape": model_wape, "baseline_wape": baseline_wape, "gap": None, "verdict": "not_evaluated"}
    gap = round(model_wape - baseline_wape, 4)
    return {
        "model_wape": model_wape,
        "baseline_wape": baseline_wape,
        "gap": gap,
        "verdict": "model_leads" if model_wape <= baseline_wape else "baseline_leads",
    }


def _distribution_section(data: pd.DataFrame | None, window_days: int, threshold_percent: float) -> dict[str, Any]:
    if data is None:
        return {"available": False, "window_days": window_days, "threshold_percent": threshold_percent, "overall": None, "routes": [], "flagged_count": 0}
    latest = data["service_date"].max()
    recent_start = latest - pd.Timedelta(days=window_days - 1)
    prior_start = recent_start - pd.Timedelta(days=window_days)
    recent = data[data["service_date"] >= recent_start]
    prior = data[(data["service_date"] >= prior_start) & (data["service_date"] < recent_start)]

    def summarize(recent_frame: pd.DataFrame, prior_frame: pd.DataFrame) -> dict[str, Any] | None:
        if recent_frame.empty or prior_frame.empty:
            return None
        recent_mean = float(recent_frame["passengers"].mean())
        prior_mean = float(prior_frame["passengers"].mean())
        if prior_mean <= 0:
            return None
        change = 100.0 * (recent_mean - prior_mean) / prior_mean
        return {
            "recent_mean": round(recent_mean, 2),
            "prior_mean": round(prior_mean, 2),
            "change_percent": round(change, 2),
            "flagged": abs(change) >= threshold_percent,
        }

    overall = summarize(recent, prior)
    routes = []
    for route in sorted(data["route_id"].unique()):
        summary = summarize(recent[recent["route_id"] == route], prior[prior["route_id"] == route])
        if summary is not None:
            routes.append({"route": route, **summary})
    return {
        "available": overall is not None,
        "window_days": window_days,
        "threshold_percent": threshold_percent,
        "overall": overall,
        "routes": routes[:50],
        "flagged_count": sum(1 for item in routes if item["flagged"]),
    }


def _checks(
    *,
    policy: dict[str, float],
    model_section: dict[str, Any],
    data_section: dict[str, Any],
    metrics_section: dict[str, Any],
    comparison: dict[str, Any],
    distribution: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, label: str, status: str, detail: str) -> None:
        checks.append({"name": name, "label": label, "status": status, "detail": detail})

    if not model_section["available"]:
        add("model_present", "Model present", "fail", "No trained model bundle is available.")
    else:
        add("model_present", "Model present", "pass", f"Model version {model_section['version'] or 'unknown'} is deployed.")

    if not data_section["available"]:
        add("data_validation", "Data validation", "fail", "No observations are available.")
        add("coverage", "Route coverage", "unknown", "No observations are available to measure coverage.")
    else:
        missing = data_section["missing_percent"]
        status = "pass" if missing is not None and missing <= policy["max_missing_percent"] else "fail"
        add(
            "data_validation",
            "Data validation",
            status,
            f"{missing if missing is not None else 'unknown'}% of route-days are missing (limit {policy['max_missing_percent']:g}%).",
        )
        minimum = data_section["min_observations_per_route"] or 0
        coverage_status = "pass" if minimum >= policy["min_observations_per_route"] else "fail"
        add(
            "coverage",
            "Route coverage",
            coverage_status,
            f"Lowest route has {minimum} observation(s); policy requires {policy['min_observations_per_route']:g}.",
        )

    if not metrics_section["available"]:
        add("performance", "Performance versus baseline", "unknown", "No backtest metrics are available.")
    else:
        model_wape = comparison["model_wape"]
        baseline_wape = comparison["baseline_wape"]
        if model_wape is None or baseline_wape is None:
            add("performance", "Performance versus baseline", "unknown", "WAPE is not available for the model and baseline.")
        else:
            allowed = baseline_wape + policy["max_wape_gap"]
            status = "pass" if model_wape <= allowed else "fail"
            add(
                "performance",
                "Performance versus baseline",
                status,
                f"Model WAPE {model_wape:.4f} vs baseline {baseline_wape:.4f} (allowed up to {allowed:.4f}).",
            )

    holdout = metrics_section["holdout"]
    selection_wape = metrics_section["selection"]["model"]["wape"]
    if holdout["available"] and holdout["model"]["wape"] is not None and selection_wape:
        limit = selection_wape * (1.0 + policy["max_holdout_degradation"])
        status = "pass" if holdout["model"]["wape"] <= limit else "fail"
        add(
            "holdout_stability",
            "Holdout stability",
            status,
            f"Holdout WAPE {holdout['model']['wape']:.4f} vs selection WAPE {selection_wape:.4f} (limit {limit:.4f}).",
        )
    else:
        add("holdout_stability", "Holdout stability", "unknown", "A final holdout score is not available.")

    if model_section["age_days"] is None:
        add("model_age", "Model freshness", "unknown", "The training timestamp is not available.")
    else:
        status = "pass" if model_section["age_days"] <= policy["max_model_age_days"] else "fail"
        add(
            "model_age",
            "Model freshness",
            status,
            f"Model is {model_section['age_days']:.1f} day(s) old (limit {policy['max_model_age_days']:g}).",
        )

    if data_section["age_days"] is None:
        add("data_freshness", "Observation freshness", "unknown", "The latest observation date is not available.")
    else:
        status = "pass" if data_section["age_days"] <= policy["max_data_age_days"] else "fail"
        add(
            "data_freshness",
            "Observation freshness",
            status,
            f"Latest observation is {data_section['age_days']:.1f} day(s) old (limit {policy['max_data_age_days']:g}).",
        )

    if distribution["available"]:
        flagged = distribution["flagged_count"]
        add(
            "distribution",
            "Demand distribution",
            "pass" if flagged == 0 else "warning",
            f"{flagged} route(s) moved by at least {distribution['threshold_percent']:g}% over {distribution['window_days']} day(s).",
        )
    else:
        add("distribution", "Demand distribution", "unknown", "Not enough history to compare recent and prior windows.")
    return checks


def _warnings(
    *,
    policy: dict[str, float],
    model_section: dict[str, Any],
    data_section: dict[str, Any],
    forecast_section: dict[str, Any],
    metrics_section: dict[str, Any],
    comparison: dict[str, Any],
    distribution: dict[str, Any],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []

    def add(code: str, severity: str, title: str, detail: str, suggested_action: str) -> None:
        warnings.append(
            {
                "code": code,
                "severity": severity,
                "title": title,
                "detail": detail,
                "suggested_action": suggested_action,
            }
        )

    if not model_section["available"]:
        add("no_model", "warning", "No model is deployed", "No trained model bundle was found, so no forecast can be trusted yet.", "Collect observations and train a model, then review the backtest.")
    else:
        age = model_section["age_days"]
        if age is not None and age > policy["max_model_age_days"]:
            add(
                "model_stale",
                "warning",
                "Model is older than the retraining policy allows",
                f"The model was trained {age:.1f} day(s) ago; the policy allows {policy['max_model_age_days']:g} day(s).",
                "Schedule a retrain, then compare the new backtest with the deployed model before promoting it.",
            )
        if metrics_section["available"]:
            model_wape = comparison["model_wape"]
            baseline_wape = comparison["baseline_wape"]
            if model_wape is not None and baseline_wape is not None and model_wape > baseline_wape + policy["max_wape_gap"]:
                add(
                    "underperforms_baseline",
                    "critical",
                    "Model is worse than the seasonal-naive baseline",
                    f"Model WAPE {model_wape:.4f} is above baseline WAPE {baseline_wape:.4f}.",
                    "Inspect route-level error, check recent data quality, and prefer the baseline until the model improves.",
                )
            holdout = metrics_section["holdout"]
            selection_wape = metrics_section["selection"]["model"]["wape"]
            if holdout["available"] and holdout["model"]["wape"] is not None and selection_wape:
                degrade = holdout["model"]["wape"] / selection_wape - 1.0
                if degrade > policy["max_holdout_degradation"]:
                    add(
                        "holdout_degradation",
                        "warning",
                        "Accuracy dropped on the untouched holdout",
                        f"Holdout WAPE is {degrade * 100:.1f}% higher than the model-selection WAPE.",
                        "Review recent demand shifts and outliers; retrain only after the holdout comparison is accepted.",
                    )

    if data_section["available"]:
        age = data_section["age_days"]
        if age is not None and age > policy["max_data_age_days"]:
            add(
                "data_stale",
                "critical",
                "Passenger observations are stale",
                f"The latest observation is {age:.1f} day(s) old; the policy allows {policy['max_data_age_days']:g}.",
                "Check the ingestion pipeline before trusting any forecast based on this data.",
            )
        missing = data_section["missing_percent"]
        if missing is not None and missing > policy["max_missing_percent"]:
            add(
                "missing_data",
                "warning",
                "Missing route-day records exceed the policy",
                f"{missing:.2f}% of expected route-days are missing (limit {policy['max_missing_percent']:g}%).",
                "Resolve or document the gaps; do not zero-fill unless operations confirms zero service.",
            )
    else:
        add("no_data", "critical", "No passenger observations", "The daily store is empty, so nothing can be validated.", "Ingest approved daily counts before training or forecasting.")

    if not forecast_section["available"]:
        add("no_forecast", "info", "No current forecast", "No forecast file is available for the monitored window.", "Generate a forecast after the model and data checks pass.")
    else:
        age = forecast_section["age_days"]
        if age is not None and age > policy["max_forecast_age_days"]:
            add(
                "forecast_stale",
                "warning",
                "Forecast has not been refreshed",
                f"The latest forecast was generated {age:.1f} day(s) ago (limit {policy['max_forecast_age_days']:g}).",
                "Regenerate the forecast with the current model and observations.",
            )

    if distribution["available"] and distribution["flagged_count"]:
        names = ", ".join(item["route"] for item in distribution["routes"] if item["flagged"])
        add(
            "distribution_shift",
            "warning",
            "Demand distribution has changed",
            f"{distribution['flagged_count']} route(s) moved by at least {distribution['threshold_percent']:g}% between the recent and prior {distribution['window_days']}-day windows: {names[:200]}.",
            "Confirm the change is real before retraining; a data fault can look like a demand shift.",
        )
    return sorted(warnings, key=lambda item: SEVERITY_ORDER.get(item["severity"], 9))


def _retraining(
    *,
    policy: dict[str, float],
    checks: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    failing = [check for check in checks if check["status"] == "fail"]
    unknown = [check for check in checks if check["status"] == "unknown"]
    if failing:
        recommendation = "retrain_candidate"
        summary = (
            f"Retraining is worth reviewing: {len(failing)} policy check(s) failed. "
            "No retraining happens automatically."
        )
    elif unknown:
        recommendation = "insufficient_information"
        summary = f"Not enough evidence to judge retraining: {len(unknown)} check(s) could not be evaluated."
    else:
        recommendation = "hold"
        summary = "All retraining-policy checks pass. Keep the current model and continue monitoring."
    return {
        "policy": policy,
        "recommendation": recommendation,
        "summary": summary,
        "failing_checks": [check["name"] for check in failing],
        "required_checks": checks,
        "reasons": [warning["title"] for warning in warnings if warning["severity"] in {"critical", "warning"}],
        "auto_retrain": False,
        "requires_manual_approval": True,
        "note": (
            "This module never retrains. A retrain must pass data validation and a time-based "
            "performance comparison with the seasonal-naive baseline, and an operator must approve "
            "promotion of the new model."
        ),
    }


def build_monitoring_snapshot(
    *,
    data: pd.DataFrame | None = None,
    model_bundle: dict | None = None,
    metrics: dict | None = None,
    forecast: pd.DataFrame | None = None,
    forecast_updated_at: str | None = None,
    policy: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Assemble the full monitoring snapshot from already-loaded artifacts."""
    timestamp = now or dt.datetime.now(dt.timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
    resolved_policy = normalize_policy(policy)

    cleaned_data = _clean_frame(data)
    model_section = _model_section(model_bundle, timestamp)
    data_section = _data_section(cleaned_data, timestamp)
    forecast_section = _forecast_section(forecast, forecast_updated_at, timestamp)
    metrics_section = _metrics_section(metrics)
    comparison = _baseline_comparison(metrics_section)
    distribution = _distribution_section(
        cleaned_data,
        int(resolved_policy["distribution_window_days"]),
        resolved_policy["distribution_shift_percent"],
    )
    checks = _checks(
        policy=resolved_policy,
        model_section=model_section,
        data_section=data_section,
        metrics_section=metrics_section,
        comparison=comparison,
        distribution=distribution,
    )
    warnings = _warnings(
        policy=resolved_policy,
        model_section=model_section,
        data_section=data_section,
        forecast_section=forecast_section,
        metrics_section=metrics_section,
        comparison=comparison,
        distribution=distribution,
    )
    return {
        "generated_at": timestamp.isoformat(),
        "model": model_section,
        "training_coverage": {
            "trained_through": model_section["trained_through"],
            "route_count": data_section["route_count"],
            "first_date": data_section["first_date"],
            "last_observed_date": data_section["last_observed_date"],
            "span_days": data_section["span_days"],
            "min_observations_per_route": data_section["min_observations_per_route"],
        },
        "data": data_section,
        "forecast": forecast_section,
        "metrics": metrics_section,
        "baseline_comparison": comparison,
        "distribution": distribution,
        "warnings": warnings,
        "retraining": _retraining(policy=resolved_policy, checks=checks, warnings=warnings),
    }
