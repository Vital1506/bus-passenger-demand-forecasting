"""Unit tests for data quality and distribution drift monitoring engine."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.data_quality import calculate_psi, DataQualityMonitor, get_quality_monitor


def test_psi_identical_distributions():
    np.random.seed(42)
    expected = np.random.normal(1000, 100, 500)
    actual = np.random.normal(1000, 100, 200)

    psi = calculate_psi(expected, actual)
    assert psi < 0.10  # Identical distributions should have minimal PSI


def test_psi_shifted_distribution():
    np.random.seed(42)
    expected = np.random.normal(1000, 100, 500)
    # Significant shift: mean doubled
    actual = np.random.normal(2500, 200, 200)

    psi = calculate_psi(expected, actual)
    assert psi >= 0.25  # Should indicate significant drift


def test_assess_drift_stable_and_shifted_dataframe():
    np.random.seed(42)
    monitor = DataQualityMonitor()

    # Stable dataframe
    dates = pd.date_range("2024-01-01", periods=300, freq="D")
    counts = np.random.normal(500, 30, 300)
    df_stable = pd.DataFrame({"service_date": dates, "passengers": counts})

    rep_stable = monitor.assess_drift(df_stable)
    assert rep_stable.drift_level == "STABLE"
    assert rep_stable.retraining_recommended is False

    # Shifted dataframe (last 75 dates have massive spike)
    counts_shifted = counts.copy()
    counts_shifted[225:] = counts_shifted[225:] * 3.5  # 3.5x shift
    df_shifted = pd.DataFrame({"service_date": dates, "passengers": counts_shifted})

    rep_shifted = monitor.assess_drift(df_shifted)
    assert rep_shifted.drift_level in {"MODERATE", "SIGNIFICANT"}
    assert len(monitor.get_audit_logs()) >= 2


def test_data_quality_api_endpoints():
    import src.app as app_mod

    paths = {r.path for r in app_mod.app.routes}
    assert "/api/quality/drift-status" in paths
    assert "/api/quality/audit-logs" in paths
    assert "/api/quality/check-drift" in paths


def test_c7_residual_psi_drift():
    """Verify C7: PSI on forecast residuals detects model degradation and recommends retraining."""
    np.random.seed(42)
    monitor = DataQualityMonitor()

    # Stable residuals: mean zero
    val_res = np.random.normal(0, 20, 200)
    prod_res_stable = np.random.normal(0, 22, 100)
    res_stable = monitor.assess_residual_drift(val_res, prod_res_stable)
    assert res_stable["drift_level"] == "STABLE"
    assert res_stable["retraining_recommended"] is False

    # Degraded model: large residual bias/variance drift
    prod_res_drifted = np.random.normal(80, 50, 100)
    res_drifted = monitor.assess_residual_drift(val_res, prod_res_drifted)
    assert res_drifted["drift_level"] == "SIGNIFICANT"
    assert res_drifted["retraining_recommended"] is True

    import src.app as app_mod
    drift_stat = app_mod.get_quality_drift_status()
    assert "psi_score" in drift_stat
    assert "drift_level" in drift_stat

    logs = app_mod.get_quality_audit_logs()
    assert isinstance(logs, list)

    forced = app_mod.check_quality_drift()
    assert "psi_score" in forced
