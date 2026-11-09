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
        all_predictions.append(scored)
    joined = pd.concat(all_predictions, ignore_index=True)
    actual_values = joined["passengers"].to_numpy(dtype=float)
    model_metrics = metrics(actual_values, joined["predicted_passengers"].to_numpy(dtype=float))
    baseline_metrics = metrics(actual_values, joined["baseline"].to_numpy(dtype=float))
    return joined, model_metrics, baseline_metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, help="CSV with service_date, route_id, passengers")
    parser.add_argument("--model-out", default="artifacts/model.joblib")
    parser.add_argument("--horizon", type=int, default=7)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--missing-days", choices=["error", "zero"], default="error")
    parser.add_argument("--metrics-out", default="artifacts/backtest_metrics.json")
    parser.add_argument("--predictions-out", default="artifacts/backtest_predictions.csv")
    args = parser.parse_args()
    if args.horizon < 1 or args.folds < 1:
        parser.error("--horizon and --folds must be positive")
    if args.horizon > 90:
        parser.error("--horizon cannot exceed 90 days")
    raw = load_data(args.data)
    data = complete_daily_series(raw, missing_days=args.missing_days)
    backtest, model_scores, baseline_scores = rolling_backtest(data, args.horizon, args.folds)
    print(f"Rolling-origin backtest: {args.folds} folds × {args.horizon} days")
    for label, score in (("ML model", model_scores), ("Seasonal-naive baseline", baseline_scores)):
        print(f"{label}: MAE={score['mae']:.2f} RMSE={score['rmse']:.2f} WAPE={score['wape']:.2%}")
    if model_scores["wape"] > baseline_scores["wape"]:
        print("WARNING: ML model underperformed the seasonal-naive baseline on WAPE; inspect route-level metrics before deployment.")

    route_metrics = {}
    for route, group in backtest.groupby("route_id"):
        route_metrics[str(route)] = {
            "model": metrics(group["passengers"].to_numpy(), group["predicted_passengers"].to_numpy()),
            "seasonal_naive": metrics(group["passengers"].to_numpy(), group["baseline"].to_numpy()),
        }
        print(f"Route {route}: model WAPE={route_metrics[str(route)]['model']['wape']:.2%}; seasonal-naive WAPE={route_metrics[str(route)]['seasonal_naive']['wape']:.2%}")

    metrics_path = Path(args.metrics_out)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps({
        "backtest_folds": args.folds, "horizon_days": args.horizon,
        "model": model_scores, "seasonal_naive": baseline_scores,
        "by_route": route_metrics,
    }, indent=2), encoding="utf-8")
    predictions_path = Path(args.predictions_out)
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    backtest.to_csv(predictions_path, index=False)

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
        "horizon": args.horizon,
        "model_version": "0.2.0",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_sha256": hashlib.sha256(Path(args.data).read_bytes()).hexdigest(),
        "package_versions": {"pandas": pd.__version__, "scikit_learn": sklearn.__version__, "joblib": joblib.__version__},
        "interval_method": "rolling-origin 90th percentile absolute residual; symmetric and approximate",
        "global_error_band_90": global_band,
        "route_error_band_90": route_bands,
        "backtest_model_metrics": model_scores,
        "backtest_baseline_metrics": baseline_scores,
        "trained_through": data["service_date"].max().date().isoformat(),
    }
    output = Path(args.model_out)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, output)
    print(f"Saved model bundle: {output}")
    print(f"Saved backtest metrics: {metrics_path}")
    print(f"Saved backtest predictions: {predictions_path}")


if __name__ == "__main__":
    main()
