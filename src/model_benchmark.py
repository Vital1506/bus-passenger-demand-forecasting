"""Multi-model machine learning benchmarking, evaluation leaderboard, and empirical prediction interval calibration.

Supported Model Architectures:
- HistGradientBoostingRegressor (Gradient Boosted Trees - primary baseline)
- RandomForestRegressor (Bagged ensemble of decision trees)
- ExtraTreesRegressor (Extremely Randomized Trees)
- SeasonalNaive (Non-parametric persistence baseline)

Strict Methodological Rules:
- Enforces strict rolling-origin chronological backtesting without future data leakage
- Evaluates metrics on untouched holdout folds: MAE, RMSE, WAPE
- Calibrates empirical prediction intervals directly from out-of-fold historical residual quantiles
- Recommends model promotion only with verifiable holdout performance improvement
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from .forecasting import FEATURE_COLUMNS, add_features, complete_daily_series, wape
from .modeling import recursive_forecast, seasonal_naive_forecast


def build_pipeline(model_type: Literal["hist_gradient_boosting", "random_forest", "extra_trees"] = "hist_gradient_boosting") -> Pipeline:
    """Instantiate a scikit-learn pipeline for the specified regressor variant."""
    preprocessing = ColumnTransformer(
        [("route", OneHotEncoder(handle_unknown="ignore", sparse_output=False), ["route_id"])],
        remainder="passthrough",
        verbose_feature_names_out=False,
    )

    if model_type == "random_forest":
        regressor = RandomForestRegressor(
            n_estimators=100,
            max_depth=12,
            min_samples_split=4,
            random_state=42,
            n_jobs=-1,
        )
    elif model_type == "extra_trees":
        regressor = ExtraTreesRegressor(
            n_estimators=100,
            max_depth=12,
            min_samples_split=4,
            random_state=42,
            n_jobs=-1,
        )
    else:
        regressor = HistGradientBoostingRegressor(
            learning_rate=0.06,
            max_iter=300,
            max_leaf_nodes=15,
            l2_regularization=2.0,
            loss="squared_error",
            random_state=42,
        )

    return Pipeline([("features", preprocessing), ("model", regressor)])


def compute_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    return {
        "mae": round(float(mean_absolute_error(actual, predicted)), 2),
        "rmse": round(float(mean_squared_error(actual, predicted) ** 0.5), 2),
        "wape": round(float(wape(actual, predicted)), 4),
    }


def calibrate_empirical_intervals(residuals: np.ndarray) -> tuple[float, float]:
    """Calculate empirical 90% error intervals (5th and 95th percentiles) from historical residuals."""
    if len(residuals) < 10:
        std = float(np.std(residuals)) if len(residuals) > 0 else 50.0
        return -1.645 * std, 1.645 * std

    lower_offset = float(np.percentile(residuals, 5.0))
    upper_offset = float(np.percentile(residuals, 95.0))
    return round(lower_offset, 2), round(upper_offset, 2)


def run_multi_model_benchmark(
    data: pd.DataFrame,
    horizon: int = 7,
    folds: int = 2,
    candidate_models: list[str] | None = None,
) -> dict[str, Any]:
    """Run rolling-origin backtest across model candidates and return benchmark leaderboard."""
    if horizon < 1 or horizon > 90:
        raise ValueError("horizon must be between 1 and 90 days")
    if folds < 1:
        raise ValueError("folds must be at least 1")
    required_columns = {"service_date", "route_id", "passengers"}
    missing_columns = required_columns - set(data.columns)
    if missing_columns:
        raise ValueError(f"Benchmark data is missing required columns: {', '.join(sorted(missing_columns))}")
    if data.empty:
        raise ValueError("Benchmark data contains no observations")
    candidates = list(candidate_models) if candidate_models is not None else [
        "hist_gradient_boosting", "random_forest", "extra_trees"
    ]
    allowed_candidates = {"hist_gradient_boosting", "random_forest", "extra_trees"}
    unknown_candidates = set(candidates) - allowed_candidates
    if unknown_candidates:
        raise ValueError(f"Unsupported candidate model(s): {', '.join(sorted(unknown_candidates))}")
    if len(candidates) != len(set(candidates)):
        raise ValueError("Candidate model names must be unique")
    data = complete_daily_series(data, missing_days="error")
    dates = pd.DatetimeIndex(sorted(data["service_date"].unique()))
    needed = max(90, 29 + horizon * folds)
    if len(dates) < needed:
        raise ValueError(f"Need at least {needed} continuous calendar days for {folds} folds at horizon {horizon}; found {len(dates)}")

    leaderboard = []
    all_residuals: dict[str, list[float]] = {m: [] for m in candidates}
    all_residuals["seasonal_naive"] = []

    # Evaluate seasonal naive baseline
    naive_predictions = []
    for fold in range(folds, 0, -1):
        origin = dates[-(horizon * fold + 1)]
        train_hist = data[data["service_date"] <= origin].copy()
        actual = data[(data["service_date"] > origin) & (data["service_date"] <= origin + timedelta(days=horizon))].copy()
        baseline = seasonal_naive_forecast(train_hist, horizon)
        scored = actual.merge(baseline, on=["service_date", "route_id"], how="left", validate="one_to_one")
        if scored["baseline"].isna().any():
            raise ValueError("Seasonal-naive benchmark did not cover every actual route/date row")
        scored["residual"] = scored["passengers"] - scored["baseline"]
        all_residuals["seasonal_naive"].extend(scored["residual"].tolist())
        naive_predictions.append(scored)

    naive_df = pd.concat(naive_predictions, ignore_index=True)
    naive_m = compute_metrics(naive_df["passengers"].to_numpy(), naive_df["baseline"].to_numpy())
    naive_low, naive_high = calibrate_empirical_intervals(np.array(all_residuals["seasonal_naive"]))
    leaderboard.append({
        "model_name": "seasonal_naive",
        "model_type": "baseline",
        "mae": naive_m["mae"],
        "rmse": naive_m["rmse"],
        "wape": naive_m["wape"],
        "empirical_interval_90": [naive_low, naive_high],
        "is_champion": False,
    })

    champion_model = "seasonal_naive"
    lowest_wape = naive_m["wape"]

    # Evaluate each ML candidate
    for model_name in candidates:
        model_preds = []
        for fold in range(folds, 0, -1):
            origin = dates[-(horizon * fold + 1)]
            train_hist = data[data["service_date"] <= origin].copy()
            actual = data[(data["service_date"] > origin) & (data["service_date"] <= origin + timedelta(days=horizon))].copy()

            supervised = add_features(train_hist).dropna(subset=FEATURE_COLUMNS)
            pipeline = build_pipeline(model_name)
            pipeline.fit(supervised[FEATURE_COLUMNS], supervised["passengers"])

            pred = recursive_forecast(pipeline, train_hist, horizon)
            scored = actual.merge(pred, on=["service_date", "route_id"], how="left", validate="one_to_one")
            if scored["predicted_passengers"].isna().any():
                raise ValueError(f"{model_name} did not forecast every actual route/date row")
            scored["residual"] = scored["passengers"] - scored["predicted_passengers"]
            all_residuals[model_name].extend(scored["residual"].tolist())
            model_preds.append(scored)

        model_df = pd.concat(model_preds, ignore_index=True)
        m_metrics = compute_metrics(model_df["passengers"].to_numpy(), model_df["predicted_passengers"].to_numpy())
        low_res, high_res = calibrate_empirical_intervals(np.array(all_residuals[model_name]))

        entry = {
            "model_name": model_name,
            "model_type": "ml_regressor",
            "mae": m_metrics["mae"],
            "rmse": m_metrics["rmse"],
            "wape": m_metrics["wape"],
            "empirical_interval_90": [low_res, high_res],
            "is_champion": False,
        }
        if m_metrics["wape"] < lowest_wape:
            lowest_wape = m_metrics["wape"]
            champion_model = model_name
        leaderboard.append(entry)

    # Sort leaderboard by WAPE ascending (lowest error first)
    leaderboard.sort(key=lambda x: x["wape"])
    for entry in leaderboard:
        if entry["model_name"] == champion_model:
            entry["is_champion"] = True

    return {
        "status": "COMPLETED",
        "horizon_days": horizon,
        "folds_evaluated": folds,
        "champion_model": champion_model,
        "leaderboard": leaderboard,
    }
