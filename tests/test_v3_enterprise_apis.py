"""Integration tests verifying Phase 3 through 12 Enterprise REST APIs."""

from __future__ import annotations

import io
import pytest
from starlette.datastructures import UploadFile
from fastapi import HTTPException

import src.app as app_mod


def test_realtime_providers_api():
    providers = app_mod.list_realtime_providers()
    assert isinstance(providers, list)
    assert len(providers) >= 1
    cta = [p for p in providers if p["agency_id"] == "CTA"][0]
    assert "status" in cta
    assert cta["status"] in {"DISCONNECTED", "CONNECTED", "STALE", "ERROR"}
    # Verify no secret credentials are leaked in response
    assert "auth_token" not in cta
    assert "api_key" not in cta


def test_service_simulator_api():
    req = app_mod.SimulationRequest(
        route_id="R10",
        forecast_hourly_demand=250.0,
        baseline_trips_per_hour=4.0,
        modified_trips_per_hour=5.0,
        vehicle_capacity=55,
        operating_hours=16,
    )
    res = app_mod.evaluate_service_simulation(req)
    assert res["route_id"] == "R10"
    assert res["baseline_capacity_per_hour"] == 220
    assert res["modified_capacity_per_hour"] == 275
    assert res["baseline_utilization_pct"] == round(250.0 / 220.0 * 100, 1)
    assert res["modified_utilization_pct"] == round(250.0 / 275.0 * 100, 1)


def test_reports_api(tmp_path, monkeypatch):
    db_path = tmp_path / "test_reports.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("BUS_FORECAST_DATA_DIR", str(tmp_path / "data"))
    app_mod.init_db()

    rep = app_mod.get_daily_report(date="2026-11-01", agency_id="CTA")
    assert rep["report_type"] == "DAILY_PASSENGER_SUMMARY"
    assert rep["service_date"] == "2026-11-01"

    csv_resp = app_mod.download_daily_report_csv(date="2026-11-01", agency_id="CTA")
    assert csv_resp.media_type == "text/csv"

    risk_rep = app_mod.get_capacity_risk_report(agency_id="CTA")
    assert risk_rep["report_type"] == "CAPACITY_RISK_REPORT"
    assert "total_risk_incidents" in risk_rep


def test_occupancy_api(tmp_path, monkeypatch):
    db_path = tmp_path / "test_occ.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("BUS_FORECAST_DATA_DIR", str(tmp_path / "data"))
    app_mod.init_db()

    route_occ = app_mod.get_route_occupancy_profile(route_id="R10", agency_id="CTA")
    assert route_occ["route_id"] == "R10"
    assert route_occ["status"] == "NO_APC_DATA"

    # Trip occupancy 404 when absent
    with pytest.raises(HTTPException) as exc_info:
        app_mod.get_trip_occupancy(trip_id="UNKNOWN_TRIP", agency_id="CTA")
    assert exc_info.value.status_code == 404
