"""Model definition and recursive multi-day forecast utilities."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from .forecasting import FEATURE_COLUMNS, add_features


def build_model() -> Pipeline:
    preprocessing = ColumnTransformer(
        [("route", OneHotEncoder(handle_unknown="ignore", sparse_output=False), ["route_id"])],
        remainder="passthrough", verbose_feature_names_out=False,
    )
    regressor = HistGradientBoostingRegressor(
        learning_rate=0.06, max_iter=300, max_leaf_nodes=15,
        l2_regularization=2.0, loss="squared_error", random_state=42,
    )
    return Pipeline([("features", preprocessing), ("model", regressor)])


def recursive_forecast(model: Pipeline, history: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Forecast each date using only observations and earlier predictions."""
    working = history.copy().sort_values(["route_id", "service_date"]).reset_index(drop=True)
    routes = sorted(working["route_id"].astype(str).unique())
    last_date = pd.to_datetime(working["service_date"]).max()
    output = []
    for step in range(1, horizon + 1):
        forecast_date = last_date + pd.Timedelta(days=step)
        future = pd.DataFrame({"service_date": [forecast_date] * len(routes), "route_id": routes, "passengers": [np.nan] * len(routes)})
        feature_frame = add_features(pd.concat([working, future], ignore_index=True))
        current = feature_frame[feature_frame["service_date"].eq(forecast_date)].copy()
        if current[FEATURE_COLUMNS].isna().any().any():
            raise ValueError("Insufficient prior history to forecast; at least 28 continuous observations per route are required")
        values = np.maximum(0.0, model.predict(current[FEATURE_COLUMNS]))
        predicted = pd.DataFrame({"service_date": forecast_date, "route_id": routes, "passengers": values})
        working = pd.concat([working, predicted], ignore_index=True).sort_values(["route_id", "service_date"]).reset_index(drop=True)
        output.extend({"service_date": forecast_date, "route_id": route, "predicted_passengers": float(value)} for route, value in zip(routes, values))
    return pd.DataFrame(output)


def seasonal_naive_forecast(history: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Repeat the latest same-weekday value, recursively for horizons over 7 days."""
    working = history.copy().sort_values(["route_id", "service_date"]).reset_index(drop=True)
    last_date = pd.to_datetime(working["service_date"]).max()
    output = []
    for step in range(1, horizon + 1):
        forecast_date = last_date + pd.Timedelta(days=step)
        lookup = working.set_index(["service_date", "route_id"])["passengers"]
        rows = []
        for route in sorted(working["route_id"].astype(str).unique()):
            previous_week = forecast_date - pd.Timedelta(days=7)
            value = float(lookup.loc[(previous_week, route)])
            rows.append({"service_date": forecast_date, "route_id": route, "baseline": value})
        daily = pd.DataFrame(rows)
        working = pd.concat([working, daily.rename(columns={"baseline": "passengers"})], ignore_index=True).sort_values(["route_id", "service_date"]).reset_index(drop=True)
        output.extend(rows)
    return pd.DataFrame(output)
