"""Tests for system bootstrap, demo data initialization, readiness probe, and forecast caching."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.app import (
    app,
    ensure_system_initialized,
    project_path,
    public_demo_status,
    readiness,
    status_payload,
)


from src.db.session import get_engine

@pytest.fixture
def clean_test_env():
    """Create an isolated temporary data directory for testing bootstrap."""
    td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    tmp_path = Path(td.name)
    try:
        with patch.dict(os.environ, {"BUS_FORECAST_DATA_DIR": str(tmp_path)}):
            with patch("src.app.DATA_DIR", tmp_path):
                with patch("src.app.ARTIFACT_DIR", tmp_path / "artifacts"):
                    with patch("src.app.DATA_PATH", tmp_path / "daily_ridership.csv"):
                        with patch("src.app.MODEL_PATH", tmp_path / "artifacts" / "model.joblib"):
                            with patch("src.app.METRICS_PATH", tmp_path / "artifacts" / "backtest_metrics.json"):
                                with patch("src.app.FORECAST_PATH", tmp_path / "forecast.csv"):
                                    with patch("src.app.FORECAST_META_PATH", tmp_path / "forecast_metadata.json"):
                                        with patch("src.app.META_PATH", tmp_path / "data_source.json"):
                                            yield tmp_path
    finally:
        try:
            get_engine().dispose()
        except Exception:
            pass
        td.cleanup()


def test_ensure_system_initialized_bootstraps_sample_data(clean_test_env):
    """Verify ensure_system_initialized bootstraps sample data and model artifacts."""
    data_path = clean_test_env / "daily_ridership.csv"
    model_path = clean_test_env / "artifacts" / "model.joblib"
    forecast_path = clean_test_env / "forecast.csv"

    assert not data_path.exists()
    assert not model_path.exists()

    ensure_system_initialized()

    # Data file bootstrapped and valid
    assert data_path.exists()
    df = pd.read_csv(data_path)
    assert len(df) == 2190
    assert set(df["route_id"].unique()) == {"R10", "R22", "R35"}
    assert "service_date" in df.columns
    assert "passengers" in df.columns

    # Model and forecast bootstrapped
    assert model_path.exists()
    assert forecast_path.exists()
    fdf = pd.read_csv(forecast_path)
    assert len(fdf) == 21
    assert set(fdf["route_id"].unique()) == {"R10", "R22", "R35"}


def test_readiness_probe_reports_ready_state(clean_test_env):
    """Readiness endpoint returns 200 with data_loaded, model_ready, and routes_available."""
    ensure_system_initialized()
    client = TestClient(app)
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["data_loaded"] is True
    assert data["model_ready"] is True
    assert data["forecasts_ready"] is True
    assert data["routes_available"] == 3


def test_public_demo_status_returns_submillisecond(clean_test_env):
    """public_demo_status returns from cache instantly without retraining."""
    import time

    ensure_system_initialized()
    t0 = time.time()
    result = public_demo_status(days=7)
    elapsed = time.time() - t0

    assert elapsed < 0.2  # Should be sub-second
    assert result["data_loaded"] is True
    assert result["rows"] == 2190
    assert result["route_count"] == 3
    assert len(result["forecasts"]) == 21
    assert result["public_demo"] is True

def test_readiness_requires_forecast_artifact(clean_test_env, monkeypatch):
    """A live database and model are not ready until the forecast is available."""
    import src.app as app_module

    data_path = clean_test_env / "daily_ridership.csv"
    model_path = clean_test_env / "artifacts" / "model.joblib"
    forecast_path = clean_test_env / "forecast.csv"
    data_path.write_text("service_date,route_id,passengers\n2026-11-01,R10,10\n", encoding="utf-8")
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"placeholder")
    assert not forecast_path.exists()

    monkeypatch.setattr(app_module, "PUBLIC_DEMO_CACHE", None)
    monkeypatch.setattr(app_module, "check_db_health", lambda: {"connected": True})
    monkeypatch.setattr(app_module, "check_storage_health", lambda: {"writable": True})

    response = readiness()
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["data_loaded"] is True
    assert payload["model_ready"] is True
    assert payload["forecasts_ready"] is False
