"""Unit tests for Phase 10 Reporting & Webhooks Engine."""

from __future__ import annotations

from datetime import date
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.session import init_db
from src.db.models import DailyPassengerRecord, OperationalAlertRecord
from src.reporting import (
    export_daily_report_csv,
    generate_capacity_risk_report,
    generate_daily_passenger_report,
    is_safe_webhook_url,
    send_alert_webhook,
)


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        yield session


def test_generate_daily_passenger_report_and_csv(test_db):
    test_db.add_all([
        DailyPassengerRecord(agency_id="CTA", service_date=date(2026, 11, 1), route_id="R10", passengers=250.0, source_system="apc"),
        DailyPassengerRecord(agency_id="CTA", service_date=date(2026, 11, 1), route_id="R20", passengers=350.0, source_system="apc"),
    ])
    test_db.commit()

    report = generate_daily_passenger_report(test_db, "2026-11-01", agency_id="CTA")
    assert report["report_type"] == "DAILY_PASSENGER_SUMMARY"
    assert report["total_boardings"] == 600.0
    assert report["active_routes_count"] == 2
    assert report["average_passengers_per_route"] == 300.0

    csv_text = export_daily_report_csv(report)
    assert "service_date,route_id,passengers,source_system" in csv_text
    assert "2026-11-01,R20,350.0,apc" in csv_text


def test_capacity_risk_report(test_db):
    test_db.add(
        OperationalAlertRecord(
            agency_id="CTA",
            alert_id="ALT-CAP-1",
            alert_type="OVERCROWDING",
            severity="CRITICAL",
            route_id="R10",
            summary="Vehicle at 115% capacity",
        )
    )
    test_db.commit()

    rep = generate_capacity_risk_report(test_db, agency_id="CTA")
    assert rep["report_type"] == "CAPACITY_RISK_REPORT"
    assert rep["total_risk_incidents"] == 1
    assert rep["incidents"][0]["alert_id"] == "ALT-CAP-1"


def test_send_alert_webhook(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", 443))])

    class MockClient:
        def post(self, url, json):
            class MockResponse:
                status_code = 200
            return MockResponse()

    success = send_alert_webhook("https://webhook.site/test", {"alert": "test"}, http_client=MockClient())
    assert success is True

    # Empty url returns False
    assert send_alert_webhook("", {}) is False


def test_webhook_ssrf_protection_rejects_insecure_and_internal_targets(monkeypatch):
    monkeypatch.delenv("BUS_FORECAST_DEV_MODE", raising=False)
    monkeypatch.delenv("BUS_FORECAST_ALLOW_INSECURE_WEBHOOKS", raising=False)

    # 1. Plain HTTP is rejected
    is_safe, msg = is_safe_webhook_url("http://example.com/hook")
    assert not is_safe
    assert "HTTPS" in msg

    # 2. Loopback IP is rejected
    is_safe, msg = is_safe_webhook_url("https://127.0.0.1:8443/hook")
    assert not is_safe
    assert "loopback" in msg

    # 3. Cloud metadata IP (169.254.169.254) is rejected
    is_safe, msg = is_safe_webhook_url("https://169.254.169.254/latest/meta-data")
    assert not is_safe
    assert "link-local" in msg

    # 4. RFC 1918 Private networks rejected
    for private_target in ["10.0.0.1", "172.16.5.1", "192.168.1.100"]:
        is_safe, msg = is_safe_webhook_url(f"https://{private_target}/hook")
        assert not is_safe
        assert "private network" in msg

    # 5. Embedded credentials rejected
    is_safe, msg = is_safe_webhook_url("https://admin:pass@example.com/hook")
    assert not is_safe
    assert "credentials" in msg

    # 6. Host allowlist filtering
    monkeypatch.setenv("BUS_FORECAST_WEBHOOK_ALLOWLIST", "hooks.slack.com,api.pagerduty.com")
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", 443))])
    
    is_safe, msg = is_safe_webhook_url("https://evil-attacker.com/hook")
    assert not is_safe
    assert "not in the allowed webhook hosts" in msg

    is_safe, _ = is_safe_webhook_url("https://hooks.slack.com/services/T00/B00/X00")
    assert is_safe
