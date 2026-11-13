"""Experimental model-comparison layer built on the existing forecasting contracts.

This module is intentionally smaller than the operational training pipeline.
It compares candidate models against each other and against the seasonal-naive
baseline on a single chronological holdout block, then returns a conservative
decision note instead of blindly picking the newest model.

It does not add weather, new features, confidence intervals, charts, model
version history, or API endpoints. Those remain separate concerns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .forecasting import FEATURE_COLUMNS, add_features, wape
from .modeling import build_model, recursive_forecast, seasonal_naive_forecast


COMPARISON_METRIC = "wape"
MIN_WAPE_IMPROVEMENT = 0.0005
_CANDIDATE_REGISTRY = {
    "hist_gradient_boosting": {
        "label": "HistGradientBoostingRegressor",
        "notes": "Existing operational model; retained as an experimental candidate in comparisons.",
    },
    "random_forest": {
        "label": "RandomForestRegressor (experimental)",
        "notes": "Tree-based candidate evaluated only where it improves validated WAPE.",
    },
}
_MIN_SELECTION_BLOCK_DAYS = 90
_NEW_ROUTE_COLD_START_NEEDS_SUPERVISED_HISTORY = (
    "New-route cold-start forecasting needs separate logic and is not handled here"
)


def _candidate_builders():
    """Declared candidate models available for comparison.

    Each candidate is built from scratch inside the comparison so the comparison
    is reproducible and does not reuse an externally fitted model.
    """
    return {
        "hist_gradient_boosting": (
            "HistGradientBoostingRegressor",
            build_model(),
        ),
        "random_forest": (
            "RandomForestRegressor (experimental)",
            _rf_pipeline(),
        ),
    }


def _rf_pipeline():
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    return Pipeline([
        ("features", ColumnTransformer(
            [("route", OneHotEncoder(handle_unknown="ignore", sparse_output=False), ["route_id"])],
            remainder="passthrough",
            verbose_feature_names_out=False,
        )),
        ("model", RandomForestRegressor(
            n_estimators=300,
            max_leaf_nodes=15,
            min_samples_leaf=4,
            random_state=42,
            n_jobs=1,
        )),
    ])


def _fit_candidate(builder, supervised, feature_columns):
    return builder.fit(supervised[feature_columns], supervised["passengers"])


def _evaluate_on_block(model, train_history, holdout, horizon):
    predicted = recursive_forecast(model, train_history, horizon)
    scored = holdout.merge(
        predicted,
        on=["service_date", "route_id"],
        validate="one_to_one",
        how="left",
    )
    if scored["predicted_passengers"].isna().any():
        raise ValueError("Comparison model predictions did not cover every actual route/date row")
    return scored


def _holdout_metrics(scored, prediction_column):
    actual = scored["passengers"].to_numpy(dtype=float)
    predicted = scored[prediction_column].to_numpy(dtype=float)
    return {
        "wape": float(wape(actual, predicted)),
        "mae": float(np.mean(np.abs(actual - predicted))),
        "rmse": float(np.sqrt(np.mean((actual - predicted) ** 2))),
    }


def _comparison_cutoffs(history, horizon):
    """Pick the comparison training window and the final holdout window.

    The holdout is the last horizon days. The comparison training block must
    precede it by enough continuous history to fit and feature the selected
    candidates.
    """
    dates = pd.DatetimeIndex(sorted(history["service_date"].unique()))
    if len(dates) < _MIN_SELECTION_BLOCK_DAYS + horizon:
        raise ValueError(
            "Not enough data for comparison. Need at least "
            f"{_MIN_SELECTION_BLOCK_DAYS} continuous calendar days for the comparison "
            f"training block plus {horizon} holdout days; found {len(dates)}"
        )
    holdout_end = dates.max()
    holdout_start = holdout_end - pd.Timedelta(days=horizon - 1)
    training_end = holdout_start - pd.Timedelta(days=1)
    training_start = training_end - pd.Timedelta(days=_MIN_SELECTION_BLOCK_DAYS - 1)
    if training_start not in dates:
        raise ValueError(
            "Not enough data for comparison. The comparison training block ends on "
            f"{training_end.date()}, but history does not extend continuously back to "
            f"{training_start.date()}"
        )
    return training_start, training_end, holdout_start, holdout_end


def compare_models(
    history,
    horizon,
    metric=COMPARISON_METRIC,
    candidates=None,
    seed=42,
):
    """Compare candidate models and the seasonal-naive baseline on one holdout block.

    Parameters
    ----------
    history:
        Validated daily route history, already complete through the latest observed
        date, with service_date, route_id, passengers.
    horizon:
        Number of forecast days to evaluate in the final holdout block.
    metric:
        Comparison head-to-head metric. Only "wape" is used for promotion logic.
    candidates:
        Iterable of candidate names from the declared registry. If omitted, all
        declared experimental candidates are compared.
    seed:
        Reserved for later reproducible candidate configuration. Not currently
        needed because each candidate builder sets its own random state.

    Returns
    -------
    dict with comparison metadata, per-candidate metrics, ranking, and a
    readable decision note.
    """
    if metric != COMPARISON_METRIC:
        raise ValueError(f"Only the comparison metric '{COMPARISON_METRIC}' is supported right now")

    history = history.copy()
    if not {"service_date", "route_id", "passengers"}.issubset(history.columns):
        raise ValueError("history must contain service_date, route_id, and passengers")

    history["service_date"] = pd.to_datetime(history["service_date"]).dt.normalize()
    history["route_id"] = history["route_id"].astype(str)
    if history["passengers"].isna().any() or not np.isfinite(history["passengers"]).all():
        raise ValueError("history passengers must be finite numeric values")

    if history.duplicated(["service_date", "route_id"]).any():
        raise ValueError("history contains duplicate service_date/route_id rows")

    if history.empty:
        raise ValueError("history is empty; not enough data for comparison")

    routes = set(history["route_id"].astype(str).unique())
    candidates = _candidate_names(candidates)

    training_start, training_end, holdout_start, holdout_end = _comparison_cutoffs(history, horizon)
    train_history = history.loc[
        (history["service_date"] >= training_start) & (history["service_date"] <= training_end)
    ].copy()
    holdout = history.loc[
        (history["service_date"] >= holdout_start) & (history["service_date"] <= holdout_end)
    ].copy()

    holdout_routes = set(holdout["route_id"])
    if missing_holdout_routes := holdout_routes - set(train_history["route_id"]):
        raise ValueError(
            "Not enough data for comparison. Routes in the holdout block were not "
            f"present during the comparison training block: {sorted(missing_holdout_routes)}. "
            f"{_NEW_ROUTE_COLD_START_NEEDS_SUPERVISED_HISTORY}."
        )

    supervised = add_features(train_history).dropna(subset=FEATURE_COLUMNS)
    trained_routes = set(supervised["route_id"].astype(str))
    if missing_supervised_routes := holdout_routes - trained_routes:
        raise ValueError(
            "Not enough data for comparison. Routes in the holdout block lack sufficient "
            f"pre-holdout supervision: {sorted(missing_supervised_routes)}. "
            f"{_NEW_ROUTE_COLD_START_NEEDS_SUPERVISED_HISTORY}."
        )

    builders = _candidate_builders()
    selected_builders = {name: builders[name] for name in candidates if name in builders}
    if unknown := sorted(set(candidates) - set(selected_builders)):
        raise ValueError(f"Unknown comparison candidates: {unknown}")

    candidate_results = {}

    for name in candidates:
        model = _fit_candidate(selected_builders[name][1], supervised, FEATURE_COLUMNS)
        scored = _evaluate_on_block(model, train_history, holdout, horizon)
        candidate_results[name] = _holdout_metrics(scored, "predicted_passengers")

    baseline_scored = _evaluate_baseline(train_history, holdout, horizon)
    baseline_metrics = _holdout_metrics(baseline_scored, "baseline")

    measured = {**candidate_results, "seasonal_naive": baseline_metrics}
    ranking = sorted(measured.items(), key=lambda item: item[1][metric])
    baseline_score = baseline_metrics[metric]
    best_non_baseline = next(
        ((name, candidate_results[name][metric]) for name in candidates if name != "seasonal_naive"),
        None,
    )

    preferred = None
    if best_non_baseline is not None:
        name, score = best_non_baseline
        if baseline_score - score >= MIN_WAPE_IMPROVEMENT:
            preferred = name

    return {
        "comparison_metric": metric,
        "comparison_horizon": horizon,
        "comparison_training_start": training_start.date().isoformat(),
        "comparison_training_end": training_end.date().isoformat(),
        "holdout_start": holdout_start.date().isoformat(),
        "holdout_end": holdout_end.date().isoformat(),
        "routes_evaluated": sorted(routes),
        "baseline": {"name": "seasonal_naive", "metric": baseline_metrics},
        "candidates": {name: {"metric": candidate_results[name]} for name in candidates},
        "ranking_by_wape": [name for name, _ in ranking],
        "preferred_over_baseline": preferred,
        "decision_note": _decision_note(
            preferred=preferred,
            baseline_score=baseline_score,
            best_non_baseline=best_non_baseline,
            metric=metric,
        ),
    }


def _evaluate_baseline(train_history, holdout, horizon):
    baseline = seasonal_naive_forecast(train_history, horizon)
    scored = holdout.merge(
        baseline,
        on=["service_date", "route_id"],
        validate="one_to_one",
    )
    if scored["baseline"].isna().any():
        raise ValueError("Baseline predictions did not cover every actual route/date row")
    return scored


def _candidate_names(candidates):
    return sorted(set(candidates) if candidates else set(_CANDIDATE_REGISTRY))


def _decision_note(preferred, baseline_score, best_non_baseline, metric):
    if preferred:
        name, score = best_non_baseline
        return (
            f"On the final holdout block, '{name}' ({metric}={score:.4f}) beat the "
            f"seasonal-naive baseline ({metric}={baseline_score:.4f}) by more than the "
            f"promotion margin, so it is preferred over baseline for this comparison. "
            f"The seasonal-naive baseline is still retained as the required reference."
        )
    if best_non_baseline is None:
        return (
            f"No experimental candidate was evaluated. The seasonal-naive baseline "
            f"({metric}={baseline_score:.4f}) remains the only reference model."
        )
    name, score = best_non_baseline
    if score > baseline_score:
        return (
            f"No model beat the seasonal-naive baseline on the final holdout block "
            f"by more than the promotion margin. '{name}' ({metric}={score:.4f}) was the "
            f"best experimental candidate, but the seasonal-naive baseline "
            f"({metric}={baseline_score:.4f}) is retained as the practical reference."
        )
    return (
        f"No model beat the seasonal-naive baseline by more than the promotion margin. "
        f"'{name}' ({metric}={score:.4f}) was closest, behind the seasonal-naive baseline "
        f"({metric}={baseline_score:.4f}), so the baseline is retained as the practical reference."
    )


def candidate_registry():
    """Public read-only view of declared comparison candidates."""
    return {name: dict(**info) for name, info in _CANDIDATE_REGISTRY.items()}
