"""Validated data handling and leakage-safe features for daily route demand."""

from __future__ import annotations

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = {"service_date", "route_id", "passengers"}
LAGS = (1, 7, 14, 28)
FEATURE_COLUMNS = [
    "route_id", "day_of_week", "day_of_year_sin", "day_of_year_cos",
    "month", "is_weekend", "lag_1", "lag_7", "lag_14", "lag_28",
    "rolling_mean_7", "rolling_mean_28", "rolling_std_28",
]


def load_data(path: str) -> pd.DataFrame:
    data = pd.read_csv(path)
    missing = REQUIRED_COLUMNS - set(data.columns)
    if missing:
        raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")
    data = data[["service_date", "route_id", "passengers"]].copy()
    data["service_date"] = pd.to_datetime(data["service_date"], errors="coerce").dt.normalize()
    data["route_id"] = data["route_id"].astype("string").str.strip()
    data["passengers"] = pd.to_numeric(data["passengers"], errors="coerce")
    if data["service_date"].isna().any():
        raise ValueError("service_date contains blank or invalid dates")
    if data["route_id"].isna().any() or data["route_id"].eq("").any():
        raise ValueError("route_id contains blank values")
    data["route_id"] = data["route_id"].astype(str)
    if data["passengers"].isna().any() or not np.isfinite(data["passengers"]).all():
        raise ValueError("passengers must contain finite numeric values")
    if (data["passengers"] < 0).any():
        raise ValueError("passengers cannot be negative")
    if data.duplicated(["service_date", "route_id"]).any():
        raise ValueError("Found duplicate route/date rows; aggregate or resolve them first")
    if data.empty:
        raise ValueError("Input data contains no rows")
    return data.sort_values(["route_id", "service_date"]).reset_index(drop=True)


def complete_daily_series(data: pd.DataFrame, missing_days: str = "error") -> pd.DataFrame:
    """Create continuous calendar series; absent dates require an explicit policy."""
    if missing_days not in {"error", "zero"}:
        raise ValueError("missing_days must be 'error' or 'zero'")
    parts = []
    gaps = []
    global_end = data["service_date"].max()
    for route, group in data.groupby("route_id", sort=True):
        group = group.set_index("service_date").sort_index()
        # Extend every route to the latest system date so trailing omissions are visible too.
        idx = pd.date_range(group.index.min(), global_end, freq="D")
        missing = idx.difference(group.index)
        if len(missing):
            gaps.append(f"{route}: {len(missing)} missing date(s), first {missing[0].date()}")
        filled = group.reindex(idx)
        filled["route_id"] = route
        if missing_days == "zero":
            filled["passengers"] = filled["passengers"].fillna(0.0)
        filled.index.name = "service_date"
        parts.append(filled.reset_index())
    if gaps and missing_days == "error":
        details = "; ".join(gaps[:8])
        raise ValueError(f"Missing route/date observations detected ({details}). Resolve them or explicitly use --missing-days zero if they mean zero service/demand.")
    return pd.concat(parts, ignore_index=True).sort_values(["route_id", "service_date"]).reset_index(drop=True)


def add_features(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.sort_values(["route_id", "service_date"]).copy()
    dates = pd.to_datetime(frame["service_date"])
    frame["route_id"] = frame["route_id"].astype(str)
    frame["day_of_week"] = dates.dt.dayofweek
    frame["day_of_year_sin"] = np.sin(2 * np.pi * dates.dt.dayofyear / 365.25)
    frame["day_of_year_cos"] = np.cos(2 * np.pi * dates.dt.dayofyear / 365.25)
    frame["month"] = dates.dt.month
    frame["is_weekend"] = (dates.dt.dayofweek >= 5).astype(int)
    grouped = frame.groupby("route_id", sort=False)["passengers"]
    for lag in LAGS:
        frame[f"lag_{lag}"] = grouped.shift(lag)
    frame["rolling_mean_7"] = grouped.transform(lambda s: s.shift(1).rolling(7, min_periods=7).mean())
    frame["rolling_mean_28"] = grouped.transform(lambda s: s.shift(1).rolling(28, min_periods=28).mean())
    frame["rolling_std_28"] = grouped.transform(lambda s: s.shift(1).rolling(28, min_periods=28).std())
    return frame


def make_model_frame(data: pd.DataFrame, missing_days: str = "error") -> pd.DataFrame:
    return add_features(complete_daily_series(data, missing_days=missing_days))


def wape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Calculate WAPE and reject undefined or non-finite evaluation inputs."""
    actual_values = np.asarray(actual, dtype=float)
    predicted_values = np.asarray(predicted, dtype=float)
    if actual_values.ndim != 1 or predicted_values.ndim != 1:
        raise ValueError("WAPE inputs must be one-dimensional arrays")
    if actual_values.shape != predicted_values.shape:
        raise ValueError("WAPE actual and predicted arrays must have the same shape")
    if actual_values.size == 0:
        raise ValueError("WAPE requires at least one observation")
    if not np.isfinite(actual_values).all() or not np.isfinite(predicted_values).all():
        raise ValueError("WAPE inputs must contain only finite values")
    denominator = float(np.abs(actual_values).sum())
    numerator = float(np.abs(actual_values - predicted_values).sum())
    if denominator == 0:
        if numerator == 0:
            return 0.0
        raise ValueError("WAPE is undefined when total actual demand is zero and predictions are nonzero")
    return numerator / denominator
