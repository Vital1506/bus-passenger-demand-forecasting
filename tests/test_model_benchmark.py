"""Unit tests for Phase 5 Multi-Model ML Benchmarking and Interval Calibration."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.forecasting import load_data
from src.model_benchmark import (
    build_pipeline,
    calibrate_empirical_intervals,
    compute_metrics,
    run_multi_model_benchmark,
)


def test_build_pipeline_variants():
    hgb = build_pipeline("hist_gradient_boosting")
    rf = build_pipeline("random_forest")
    et = build_pipeline("extra_trees")

    assert hgb.named_steps["model"].__class__.__name__ == "HistGradientBoostingRegressor"
    assert rf.named_steps["model"].__class__.__name__ == "RandomForestRegressor"
    assert et.named_steps["model"].__class__.__name__ == "ExtraTreesRegressor"


def test_calibrate_empirical_intervals():
    residuals = np.array([-100.0, -50.0, -20.0, 0.0, 10.0, 25.0, 40.0, 80.0, 120.0, 200.0])
    low, high = calibrate_empirical_intervals(residuals)
    assert low < 0
    assert high > 0
    assert low < high


def test_compute_metrics():
    actual = np.array([100.0, 200.0, 300.0])
    predicted = np.array([110.0, 190.0, 310.0])
    res = compute_metrics(actual, predicted)
    assert res["mae"] == 10.0
    assert res["rmse"] == 10.0
    assert round(res["wape"], 4) == round(30.0 / 600.0, 4)


def test_benchmark_rejects_unknown_candidate_models():
    data = load_data("data/sample_daily_ridership.csv")
    with pytest.raises(ValueError, match="Unsupported candidate model"):
        run_multi_model_benchmark(data, horizon=7, folds=1, candidate_models=["not-a-real-model"])


def test_benchmark_rejects_missing_daily_observations():
    data = load_data("data/sample_daily_ridership.csv").drop(index=10).reset_index(drop=True)
    with pytest.raises(ValueError, match="Missing route/date observations"):
        run_multi_model_benchmark(data, horizon=7, folds=1, candidate_models=[])


def test_multi_model_benchmark_execution():
    data = load_data("data/sample_daily_ridership.csv")
    res = run_multi_model_benchmark(
        data,
        horizon=7,
        folds=1,
        candidate_models=["hist_gradient_boosting", "random_forest"],
    )
    assert res["status"] == "COMPLETED"
    assert "champion_model" in res
    assert len(res["leaderboard"]) >= 2
    # Verify leaderboard is sorted by WAPE ascending
    wapes = [e["wape"] for e in res["leaderboard"]]
    assert wapes == sorted(wapes)
    # Check champion flag exists
    champions = [e for e in res["leaderboard"] if e["is_champion"]]
    assert len(champions) == 1


def test_b7_empirical_intervals_raises_on_empty_residuals():
    """Verify B7: empty residuals raises ValueError instead of returning magic 50.0 fallback."""
    with pytest.raises(ValueError, match="Insufficient data"):
        calibrate_empirical_intervals(np.array([]))


def test_b6_dynamic_model_versioning(monkeypatch):
    """Verify B6: model version is derived dynamically, not hardcoded."""
    from src.train import get_model_version

    # Explicit override
    monkeypatch.setenv("BUS_FORECAST_MODEL_VERSION", "2.1.0-custom")
    assert get_model_version() == "2.1.0-custom"

    # Dynamic date-based default
    monkeypatch.delenv("BUS_FORECAST_MODEL_VERSION", raising=False)
    ver = get_model_version()
    assert ver.startswith("1.0.0+")
    assert len(ver) > 10


def test_conformal_lead_day_intervals():
    from src.model_benchmark import calibrate_lead_day_conformal_intervals

    residuals_by_lead = {
        1: [-20, -10, 0, 5, 10, 15, -5, 8, -12, 18],
        7: [-80, -40, 10, 30, 60, -50, 75, -90, 85, -20],
    }
    bounds = calibrate_lead_day_conformal_intervals(residuals_by_lead, coverage_level=0.90)
    assert 1 in bounds
    assert 7 in bounds
    # Lead day 7 should have a wider interval span than lead day 1
    span_1 = bounds[1]["upper_offset"] - bounds[1]["lower_offset"]
    span_7 = bounds[7]["upper_offset"] - bounds[7]["lower_offset"]
    assert span_7 > span_1


def test_cold_start_route_forecast():
    from src.model_benchmark import cold_start_route_forecast

    history = pd.DataFrame({
        "service_date": ["2026-11-01", "2026-11-02", "2026-11-03"],
        "route_id": ["R1", "R1", "R1"],
        "passengers": [400.0, 420.0, 410.0],
    })
    target_dates = ["2026-11-09", "2026-11-10"]
    cold = cold_start_route_forecast("NEW_ROUTE_99", target_dates, history, scheduled_trips_per_day=40)
    assert len(cold) == 2
    assert cold[0]["route_id"] == "NEW_ROUTE_99"
    assert cold[0]["is_cold_start"] is True
    assert cold[0]["predicted_passengers"] > 0
    assert cold[0]["lower_90"] < cold[0]["predicted_passengers"] < cold[0]["upper_90"]


