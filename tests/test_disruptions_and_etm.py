import io
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from src.app import app
from src.operations_engine import (
    register_disruption,
    list_disruptions,
    clear_disruptions,
    get_disruption_impact,
)
from src.hybrid_forecasting import build_intraday_route_forecast
from src.passenger_data import (
    parse_and_aggregate_etm_data,
    aggregate_etm_to_daily,
)


@pytest.fixture(autouse=True)
def cleanup_disruptions():
    clear_disruptions()
    yield
    clear_disruptions()


def test_disruption_registration_and_impact():
    # Register a disruption for route 22 from 08:00 to 11:00 with 40% reduction (factor 0.6)
    dis = register_disruption(
        route_id="22",
        start_time="2026-11-15T08:00:00+00:00",
        end_time="2026-11-15T11:00:00+00:00",
        severity="HIGH",
        impact_factor=0.6,
        description="Water main break detour on Clark St",
        agency_id="CTA",
    )
    assert dis.disruption_id.startswith("DISRUPT-")
    assert dis.route_id == "22"
    assert dis.impact_factor == 0.6

    # Test disruption listing
    active_disruptions = list_disruptions(route_id="22")
    assert len(active_disruptions) == 1
    assert active_disruptions[0].severity == "HIGH"

    # Test impact calculation at hour 9 (within window) -> 0.6
    impact_9 = get_disruption_impact(
        route_id="22",
        hour=9,
        target_date="2026-11-15",
        agency_id="CTA",
    )
    assert impact_9 == 0.6

    # Test impact calculation at hour 14 (outside window) -> 1.0
    impact_14 = get_disruption_impact(
        route_id="22",
        hour=14,
        target_date="2026-11-15",
        agency_id="CTA",
    )
    assert impact_14 == 1.0

    # Test impact on a different route -> 1.0
    impact_other = get_disruption_impact(
        route_id="79",
        hour=9,
        target_date="2026-11-15",
        agency_id="CTA",
    )
    assert impact_other == 1.0


def test_intraday_forecast_reflects_disruption():
    # Register disruption for route R10 with factor 0.5
    register_disruption(
        route_id="R10",
        start_time="2026-11-15T14:00:00+00:00",
        end_time="2026-11-15T18:00:00+00:00",
        severity="CRITICAL",
        impact_factor=0.5,
        description="Emergency bridge closure",
    )

    forecast = build_intraday_route_forecast(
        route_id="R10",
        daily_baseline_passengers=2400.0,
        target_date="2026-11-15",
        current_hour=10,
    )

    # Hour 15 should have disruption flag and impact factor 0.5
    h15 = next(item for item in forecast.hourly_profile if item["hour"] == 15)
    assert h15["has_disruption"] is True
    assert h15["disruption_impact_factor"] == 0.5

    # Hour 12 should NOT have disruption
    h12 = next(item for item in forecast.hourly_profile if item["hour"] == 12)
    assert h12["has_disruption"] is False
    assert h12["disruption_impact_factor"] == 1.0


def test_etm_parser_and_hourly_aggregation():
    # Construct raw ETM ticketing log with individual tap transactions
    etm_csv = """ticket_id,route,timestamp,quantity,stop_id
T1001,22,2026-11-15 08:14:22,1,101
T1002,22,2026-11-15 08:29:10,2,102
T1003,22,2026-11-15 08:45:00,1,103
T1004,22,2026-11-15 09:12:00,1,104
T1005,79,2026-11-15 08:05:11,3,201
T1006,22,invalid-timestamp,1,105
"""
    hourly_df, val = parse_and_aggregate_etm_data(etm_csv, agency_id="CTA")

    assert val.is_valid is True
    assert val.total_transactions == 6
    assert val.valid_transactions == 5
    assert val.rejected_transactions == 1
    assert "22" in val.routes_detected
    assert "79" in val.routes_detected

    # Route 22, hour 8 should have 1+2+1 = 4 boardings, 3 transactions
    r22_h8 = hourly_df[(hourly_df["route_id"] == "22") & (hourly_df["hour"] == 8)]
    assert len(r22_h8) == 1
    assert int(r22_h8.iloc[0]["boardings"]) == 4
    assert int(r22_h8.iloc[0]["transaction_count"]) == 3

    # Route 22, hour 9 should have 1 boarding, 1 transaction
    r22_h9 = hourly_df[(hourly_df["route_id"] == "22") & (hourly_df["hour"] == 9)]
    assert len(r22_h9) == 1
    assert int(r22_h9.iloc[0]["boardings"]) == 1

    # Route 79, hour 8 should have 3 boardings
    r79_h8 = hourly_df[(hourly_df["route_id"] == "79") & (hourly_df["hour"] == 8)]
    assert len(r79_h8) == 1
    assert int(r79_h8.iloc[0]["boardings"]) == 3

    # Test aggregation to daily totals
    daily_df = aggregate_etm_to_daily(hourly_df)
    assert len(daily_df) == 2
    r22_daily = daily_df[daily_df["route_id"] == "22"].iloc[0]
    assert int(r22_daily["boardings"]) == 5  # 4 from h8 + 1 from h9
    r79_daily = daily_df[daily_df["route_id"] == "79"].iloc[0]
    assert int(r79_daily["boardings"]) == 3


def test_disruption_api_endpoints(monkeypatch):
    monkeypatch.delenv("BUS_FORECAST_DEV_MODE", raising=False)
    monkeypatch.setenv("BUS_FORECAST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BUS_FORECAST_OPERATOR_KEY", "operator-secret-op")
    monkeypatch.setenv("BUS_FORECAST_ANALYST_KEY", "analyst-secret-an")

    client = TestClient(app)

    # 1. Unauthenticated request to POST /api/operations/disruptions should fail 401
    resp = client.post("/api/operations/disruptions", json={
        "route": "22",
        "start_time": "2026-11-15T08:00:00+00:00",
        "end_time": "2026-11-15T12:00:00+00:00",
        "severity": "HIGH",
        "impact_factor": 0.75,
    })
    assert resp.status_code == 401

    # 2. Operator authenticated request
    headers = {"Authorization": "Bearer operator-secret-op"}
    resp = client.post("/api/operations/disruptions", json={
        "route": "22",
        "start_time": "2026-11-15T08:00:00+00:00",
        "end_time": "2026-11-15T12:00:00+00:00",
        "severity": "HIGH",
        "impact_factor": 0.75,
        "description": "Detour test",
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "Service disruption registered successfully"
    assert data["disruption"]["route_id"] == "22"
    assert data["disruption"]["impact_factor"] == 0.75

    # 3. GET /api/operations/disruptions with analyst role
    analyst_headers = {"Authorization": "Bearer analyst-secret-an"}
    get_resp = client.get("/api/operations/disruptions?route_id=22", headers=analyst_headers)
    assert get_resp.status_code == 200
    dis_list = get_resp.json()
    assert len(dis_list) >= 1
    assert any(d["route_id"] == "22" for d in dis_list)


def test_etm_validate_api_endpoint(monkeypatch):
    monkeypatch.delenv("BUS_FORECAST_DEV_MODE", raising=False)
    monkeypatch.setenv("BUS_FORECAST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BUS_FORECAST_OPERATOR_KEY", "operator-secret-op")

    client = TestClient(app)
    csv_bytes = b"ticket_id,route,timestamp,quantity\nT1,49,2026-11-15 10:00:00,2\nT2,49,2026-11-15 10:30:00,1\n"

    # Operator authenticated request
    headers = {"Authorization": "Bearer operator-secret-op"}
    resp = client.post(
        "/api/data/etm/validate",
        files={"file": ("test_etm.csv", io.BytesIO(csv_bytes), "text/csv")},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "ETM ticketing data validated successfully"
    assert data["validation"]["valid_transactions"] == 2
    assert data["validation"]["total_boardings"] == 3
