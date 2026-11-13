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
