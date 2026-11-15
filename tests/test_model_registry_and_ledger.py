"""Unit tests for Phase 3 Model Registry and Forecast Ledger."""

from __future__ import annotations

from datetime import date
import pytest
from fastapi.testclient import TestClient

import src.app as app_mod
from src.auth import UserRole
from src.db.session import get_db_session
from src.db.repository import (
    register_model_artifact,
    list_model_artifacts,
    record_forecast_ledger,
    compute_forecast_accuracy,
)


def test_model_registry_lifecycle():
    with get_db_session() as session:
        # Register candidate
        m1 = register_model_artifact(
            session=session,
            agency_id="TEST_AGENCY",
            model_id="rf_test_v1",
            model_version="1.0.0",
            strategy="random_forest",
            status="candidate",
            horizon_days=7,
            trained_through=date(2026, 11, 1),
            data_sha256="abc123sha",
            storage_path="artifacts/rf_test.joblib",
            hyperparameters={"n_estimators": 100},
            metrics={"wape": 0.105},
        )
        assert m1.status == "candidate"
        assert m1.is_active is False

        # Promote candidate to active
        m2 = register_model_artifact(
            session=session,
            agency_id="TEST_AGENCY",
            model_id="rf_test_v2",
            model_version="2.0.0",
            strategy="random_forest",
            status="active",
            horizon_days=7,
            trained_through=date(2026, 11, 8),
            data_sha256="def456sha",
            storage_path="artifacts/rf_test_v2.joblib",
            metrics={"wape": 0.098},
        )
        assert m2.status == "active"
        assert m2.is_active is True

        # List records
        records = list_model_artifacts(session=session, agency_id="TEST_AGENCY")
        assert len(records) >= 2
        active_records = list_model_artifacts(session=session, agency_id="TEST_AGENCY", status="active")
        assert len(active_records) == 1
        assert active_records[0]["model_id"] == "rf_test_v2"


def test_forecast_ledger_and_accuracy():
    with get_db_session() as session:
        # Record some ledger rows
        sample_rows = [
            {
                "service_date": "2026-11-09",
                "route_id": "R10",
                "lead_day": 1,
                "predicted_passengers": 600.0,
                "seasonal_naive": 650.0,
                "actual_passengers": 610.0,
            },
            {
                "service_date": "2026-11-10",
                "route_id": "R10",
                "lead_day": 2,
                "predicted_passengers": 620.0,
                "seasonal_naive": 660.0,
                "actual_passengers": 605.0,
            },
            {
                "service_date": "2026-11-09",
                "route_id": "R22",
                "lead_day": 1,
                "predicted_passengers": 400.0,
                "seasonal_naive": 450.0,
                "actual_passengers": 415.0,
            },
        ]
        count = record_forecast_ledger(
            session=session,
            forecast_rows=sample_rows,
            agency_id="TEST_LEDGER_AGENCY",
            model_version="1.0.0",
        )
        assert count == 3

        # Compute accuracy (with <10 evaluated rows, uses backtest artifact fallback or returns honest results)
        acc = compute_forecast_accuracy(session=session, agency_id="TEST_LEDGER_AGENCY")
        assert "agency_id" in acc
        assert "overall" in acc
        assert "by_route" in acc
        assert "by_lead_day" in acc


def test_api_forecast_accuracy_endpoint(monkeypatch):
    monkeypatch.setenv("BUS_FORECAST_API_KEY", "operator-secret-token")
    client = TestClient(app_mod.app)

    # Missing auth -> 401
    res = client.get("/api/forecast/accuracy")
    assert res.status_code == 401

    # Authenticated
    headers = {"Authorization": "Bearer operator-secret-token"}
    res = client.get("/api/forecast/accuracy", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "source" in data
    assert "overall" in data


def test_api_model_registry_endpoint(monkeypatch):
    monkeypatch.setenv("BUS_FORECAST_API_KEY", "operator-secret-token")
    client = TestClient(app_mod.app)

    headers = {"Authorization": "Bearer operator-secret-token"}
    res = client.get("/api/model/registry", headers=headers)
    assert res.status_code == 200
    assert isinstance(res.json(), list)
