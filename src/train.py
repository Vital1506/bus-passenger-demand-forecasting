"""Train a model, compare it to a seasonal baseline, and calibrate ranges."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.metrics import mean_absolute_error, mean_squared_error

from .forecasting import FEATURE_COLUMNS, add_features, complete_daily_series, load_data, wape
from .modeling import build_model, recursive_forecast, seasonal_naive_forecast


def metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    return {
        "mae": float(mean_absolute_error(actual, predicted)),
        "rmse": float(mean_squared_error(actual, predicted) ** 0.5),
        "wape": float(wape(actual, predicted)),
    }


def rolling_backtest(data: pd.DataFrame, horizon: int, folds: int) -> tuple[pd.DataFrame, dict[str, float], dict[str, float]]:
    dates = pd.DatetimeIndex(sorted(data["service_date"].unique()))
    needed = max(90, 29 + horizon * folds)
    if len(dates) < needed:
        raise ValueError(f"Need at least {needed} continuous calendar days for {folds} backtest folds at horizon {horizon}; found {len(dates)}")
    all_predictions = []
    for fold in range(folds, 0, -1):
        origin = dates[-(horizon * fold + 1)]
        train_history = data[data["service_date"] <= origin].copy()
        actual = data[(data["service_date"] > origin) & (data["service_date"] <= origin + pd.Timedelta(days=horizon))].copy()
        missing_routes = set(actual["route_id"]) - set(train_history["route_id"])
        if missing_routes:
            raise ValueError(f"Routes first appear after backtest origin {origin.date()}: {', '.join(sorted(missing_routes))}. New-route cold-start forecasting needs separate logic.")
        supervised = add_features(train_history).dropna(subset=FEATURE_COLUMNS)
        trained_routes = set(supervised["route_id"].astype(str))
        untrained_routes = set(actual["route_id"].astype(str)) - trained_routes
        if untrained_routes:
            raise ValueError(f"Insufficient pre-origin history for routes: {', '.join(sorted(untrained_routes))}")
        model = build_model()
        model.fit(supervised[FEATURE_COLUMNS], supervised["passengers"])
        predicted = recursive_forecast(model, train_history, horizon)
        scored = actual.merge(predicted, on=["service_date", "route_id"], validate="one_to_one", how="left")
        if scored["predicted_passengers"].isna().any():
            raise ValueError("Backtest predictions do not cover every actual route/date row")
        baseline = seasonal_naive_forecast(train_history, horizon)
        scored = scored.merge(baseline, on=["service_date", "route_id"], validate="one_to_one")
        scored["fold"] = folds - fold + 1
        scored["origin_date"] = origin
        scored["lead_day"] = (scored["service_date"] - origin).dt.days
        all_predictions.append(scored)
    joined = pd.concat(all_predictions, ignore_index=True)
    actual_values = joined["passengers"].to_numpy(dtype=float)
    model_metrics = metrics(actual_values, joined["predicted_passengers"].to_numpy(dtype=float))
    baseline_metrics = metrics(actual_values, joined["baseline"].to_numpy(dtype=float))
    return joined, model_metrics, baseline_metrics


def final_holdout_backtest(
    data: pd.DataFrame, horizon: int, holdout_days: int
) -> tuple[pd.DataFrame, dict[str, float], dict[str, float]]:
    """Evaluate the final untouched period in sequential, horizon-sized blocks.

    At each block boundary, actual counts from earlier holdout blocks become
    available, matching a weekly operational retraining/forecasting cadence.
    No outcomes from the current block enter its fitted model or features.
    """
    if holdout_days < horizon or holdout_days % horizon:
        raise ValueError("holdout-days must be a positive multiple of horizon and at least one full horizon")
    dates = pd.DatetimeIndex(sorted(data["service_date"].unique()))
    if len(dates) <= holdout_days:
        raise ValueError(f"Need more than {holdout_days} calendar days to reserve the final holdout")
    first_holdout_date = dates[-holdout_days]
    cutoff = first_holdout_date - pd.Timedelta(days=1)
    if cutoff not in dates:
        raise ValueError("Final holdout must start immediately after a continuous training date")

    predictions = []
    for block_number, block_start in enumerate(range(0, holdout_days, horizon), start=1):
        origin = cutoff + pd.Timedelta(days=block_start)
        train_history = data[data["service_date"] <= origin].copy()
        actual = data[(data["service_date"] > origin) & (data["service_date"] <= origin + pd.Timedelta(days=horizon))].copy()
        if len(actual) != horizon * data["route_id"].nunique():
            raise ValueError(f"Final holdout block {block_number} does not contain a complete route/date panel")
        supervised = add_features(train_history).dropna(subset=FEATURE_COLUMNS)
        if set(actual["route_id"].astype(str)) - set(supervised["route_id"].astype(str)):
            raise ValueError(f"A route in holdout block {block_number} has insufficient pre-block training history")
        model = build_model().fit(supervised[FEATURE_COLUMNS], supervised["passengers"])
        predicted = recursive_forecast(model, train_history, horizon)
        scored = actual.merge(predicted, on=["service_date", "route_id"], validate="one_to_one", how="left")
        if scored["predicted_passengers"].isna().any():
            raise ValueError(f"Holdout predictions do not cover every route/date row in block {block_number}")
        baseline = seasonal_naive_forecast(train_history, horizon)
        scored = scored.merge(baseline, on=["service_date", "route_id"], validate="one_to_one")
        scored["holdout_block"] = block_number
        scored["origin_date"] = origin
        scored["lead_day"] = (scored["service_date"] - origin).dt.days
        predictions.append(scored)

    joined = pd.concat(predictions, ignore_index=True)
    actual_values = joined["passengers"].to_numpy(dtype=float)
    model_metrics = metrics(actual_values, joined["predicted_passengers"].to_numpy(dtype=float))
    baseline_metrics = metrics(actual_values, joined["baseline"].to_numpy(dtype=float))
    return joined, model_metrics, baseline_metrics


def route_metrics(predictions: pd.DataFrame) -> dict[str, dict[str, dict[str, float]]]:
    return {
        str(route): {
            "model": metrics(group["passengers"].to_numpy(), group["predicted_passengers"].to_numpy()),
            "seasonal_naive": metrics(group["passengers"].to_numpy(), group["baseline"].to_numpy()),
        }
        for route, group in predictions.groupby("route_id")
    }


def error_analysis(predictions: pd.DataFrame) -> dict:
    """Summarize error by forecast lead and list the largest misses for review."""
    frame = predictions.copy()
    frame["absolute_error"] = (frame["passengers"] - frame["predicted_passengers"]).abs()
    by_lead_day = {}
    for lead_day, group in frame.groupby("lead_day"):
        by_lead_day[str(int(lead_day))] = metrics(
            group["passengers"].to_numpy(), group["predicted_passengers"].to_numpy()
        )
    largest = frame.nlargest(min(10, len(frame)), "absolute_error")
    fields = ["service_date", "route_id", "passengers", "predicted_passengers", "baseline", "absolute_error", "lead_day"]
    if "fold" in largest:
        fields.append("fold")
    if "holdout_block" in largest:
        fields.append("holdout_block")
    return {
        "by_lead_day": by_lead_day,
        "largest_absolute_errors": largest[fields].assign(
            service_date=largest["service_date"].dt.strftime("%Y-%m-%d")
        ).to_dict(orient="records"),
    }


def train_project(
    data_path: str | Path,
    model_out: str | Path = "artifacts/model.joblib",
    horizon: int = 7,
    folds: int = 4,
    missing_days: str = "error",
    metrics_out: str | Path = "artifacts/backtest_metrics.json",
    predictions_out: str | Path = "artifacts/backtest_predictions.csv",
    holdout_days: int = 28,
    holdout_predictions_out: str | Path = "artifacts/holdout_predictions.csv",
) -> dict:
    """Train, backtest, save the model bundle, and return API-safe run details."""
    if horizon < 1 or folds < 1:
        raise ValueError("horizon and folds must be positive")
    if horizon > 90:
        raise ValueError("horizon cannot exceed 90 days")
    raw = load_data(str(data_path))
    data = complete_daily_series(raw, missing_days=missing_days)
    if holdout_days < horizon or holdout_days % horizon:
        raise ValueError("holdout-days must be a positive multiple of horizon and at least one full horizon")
    selection_data = data[data["service_date"] < data["service_date"].max() - pd.Timedelta(days=holdout_days - 1)].copy()
    selection_needed = max(90, 29 + horizon * folds)
    selection_dates = selection_data["service_date"].nunique()
    if selection_dates < selection_needed:
        raise ValueError(
            f"Need at least {selection_needed} calendar days before the final holdout plus "
            f"{holdout_days} untouched holdout days; found {selection_dates + holdout_days} total days"
        )
    backtest, model_scores, baseline_scores = rolling_backtest(selection_data, horizon, folds)
    holdout, holdout_model_scores, holdout_baseline_scores = final_holdout_backtest(data, horizon, holdout_days)
    print(f"Rolling-origin model-selection backtest: {folds} folds × {horizon} days")
    for label, score in (("ML model", model_scores), ("Seasonal-naive baseline", baseline_scores)):
        print(f"{label}: MAE={score['mae']:.2f} RMSE={score['rmse']:.2f} WAPE={score['wape']:.2%}")
    if model_scores["wape"] > baseline_scores["wape"]:
        print("WARNING: ML model underperformed the seasonal-naive baseline on WAPE; inspect route-level metrics before deployment.")

    by_route = route_metrics(backtest)
    holdout_by_route = route_metrics(holdout)
    print(f"Untouched final holdout: {holdout_days} days, refreshed every {horizon} days")
    for label, score in (("ML model", holdout_model_scores), ("Seasonal-naive baseline", holdout_baseline_scores)):
        print(f"Final holdout {label}: MAE={score['mae']:.2f} RMSE={score['rmse']:.2f} WAPE={score['wape']:.2%}")
    for route, scores in by_route.items():
        print(f"Selection route {route}: model WAPE={scores['model']['wape']:.2%}; seasonal-naive WAPE={scores['seasonal_naive']['wape']:.2%}")

    metrics_path = Path(metrics_out)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps({
        "backtest_folds": folds, "horizon_days": horizon,
        "model": model_scores, "seasonal_naive": baseline_scores,
        "by_route": by_route,
        "model_selection_period": {
            "start": selection_data["service_date"].min().date().isoformat(),
            "end": selection_data["service_date"].max().date().isoformat(),
        },
        "final_holdout": {
            "days": holdout_days,
            "refresh_interval_days": horizon,
            "start": holdout["service_date"].min().date().isoformat(),
            "end": holdout["service_date"].max().date().isoformat(),
            "model": holdout_model_scores,
            "seasonal_naive": holdout_baseline_scores,
            "by_route": holdout_by_route,
            "error_analysis": error_analysis(holdout),
        },
    }, indent=2), encoding="utf-8")
    predictions_path = Path(predictions_out)
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    backtest.to_csv(predictions_path, index=False)
    holdout_predictions_path = Path(holdout_predictions_out)
    holdout_predictions_path.parent.mkdir(parents=True, exist_ok=True)
    holdout.to_csv(holdout_predictions_path, index=False)
    error_path = metrics_path.with_name("error_analysis.json")
    error_path.write_text(json.dumps({
        "model_selection": error_analysis(backtest),
        "final_holdout": error_analysis(holdout),
    }, indent=2), encoding="utf-8")

    errors = (backtest["passengers"] - backtest["predicted_passengers"]).abs()
    global_band = float(errors.quantile(0.90))
    route_bands = {}
    for route, group in backtest.assign(abs_error=errors).groupby("route_id"):
        route_bands[str(route)] = float(group["abs_error"].quantile(0.90))

    supervised = add_features(data).dropna(subset=FEATURE_COLUMNS)
    model = build_model().fit(supervised[FEATURE_COLUMNS], supervised["passengers"])
    bundle = {
        "model": model,
        "features": FEATURE_COLUMNS,
        "routes": sorted(data["route_id"].astype(str).unique()),
        "horizon": horizon,
        "model_version": "0.3.0",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_sha256": hashlib.sha256(Path(data_path).read_bytes()).hexdigest(),
        "package_versions": {"pandas": pd.__version__, "scikit_learn": sklearn.__version__, "joblib": joblib.__version__},
        "interval_method": "rolling-origin 90th percentile absolute residual; symmetric and approximate",
        "global_error_band_90": global_band,
        "route_error_band_90": route_bands,
        "backtest_model_metrics": model_scores,
        "backtest_baseline_metrics": baseline_scores,
        "final_holdout_model_metrics": holdout_model_scores,
        "final_holdout_baseline_metrics": holdout_baseline_scores,
        "final_holdout_days": holdout_days,
        "trained_through": data["service_date"].max().date().isoformat(),
    }
    output = Path(model_out)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, output)
    print(f"Saved model bundle: {output}")
    print(f"Saved backtest metrics: {metrics_path}")
    print(f"Saved backtest predictions: {predictions_path}")
    print(f"Saved final holdout predictions: {holdout_predictions_path}")
    print(f"Saved lead-day and largest-error analysis: {error_path}")
    return {
        "horizon_days": horizon,
        "backtest_folds": folds,
        "trained_through": bundle["trained_through"],
        "routes": bundle["routes"],
        "model_metrics": model_scores,
        "seasonal_naive_metrics": baseline_scores,
        "by_route": by_route,
        "final_holdout_metrics": holdout_model_scores,
        "final_holdout_baseline_metrics": holdout_baseline_scores,
        "final_holdout_days": holdout_days,
        "model_version": bundle["model_version"],
        "created_at_utc": bundle["created_at_utc"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, help="CSV with service_date, route_id, passengers")
    parser.add_argument("--model-out", default="artifacts/model.joblib")
    parser.add_argument("--horizon", type=int, default=7)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--missing-days", choices=["error", "zero"], default="error")
    parser.add_argument("--metrics-out", default="artifacts/backtest_metrics.json")
    parser.add_argument("--predictions-out", default="artifacts/backtest_predictions.csv")
    parser.add_argument("--holdout-days", type=int, default=28)
    parser.add_argument("--holdout-predictions-out", default="artifacts/holdout_predictions.csv")
    args = parser.parse_args()
    try:
        train_project(
            data_path=args.data,
            model_out=args.model_out,
            horizon=args.horizon,
            folds=args.folds,
            missing_days=args.missing_days,
            metrics_out=args.metrics_out,
            predictions_out=args.predictions_out,
            holdout_days=args.holdout_days,
            holdout_predictions_out=args.holdout_predictions_out,
        )
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

