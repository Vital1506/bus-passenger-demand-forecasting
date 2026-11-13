"""Invented fixtures for read-only monthly route explorer endpoints."""

from __future__ import annotations

import csv
import importlib
import io

import pytest
from fastapi import HTTPException

from src.monthly_data import MonthlyRidershipStore


def monthly_csv(*rows: tuple[str, str, str, str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Month_Beginning", "route", "routename", "MonthTotal"))
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


@pytest.fixture
def route_api(monkeypatch, tmp_path):
    monkeypatch.setenv("BUS_FORECAST_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("BUS_FORECAST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BUS_FORECAST_API_KEY", "route-test-key")
    app_module = importlib.import_module("src.app")
    store_path = tmp_path / "app-data" / "monthly" / "cta_monthly_ridership.json"
    monkeypatch.setattr(app_module, "MONTHLY_DATA_PATH", store_path)
    return app_module, store_path


@pytest.fixture
def populated_routes(route_api):
    app_module, path = route_api
    MonthlyRidershipStore(path).import_csv(
        monthly_csv(
            ("01/01/2024", "A-10", "Crosstown", "10"),
            ("03/01/2024", "A-10", "North Connector", "30"),
            ("02/01/2024", "B2", "Crosstown Express", "20"),
        ),
        "invented-route-fixture.csv",
    )
    return app_module


def test_search_by_route_id(populated_routes):
    result = populated_routes.list_monthly_routes(q="a-1", limit=25)

    assert result["total_matches"] == 1
    assert result["routes"][0]["route_id"] == "A-10"
    assert result["routes"][0]["latest_boardings"] == "30"


def test_search_by_route_name_is_case_insensitive(populated_routes):
    result = populated_routes.list_monthly_routes(q="cRoSsToWn", limit=25)

    assert [route["route_id"] for route in result["routes"]] == ["A-10", "B2"]


def test_search_finds_historical_route_names(populated_routes):
    result = populated_routes.list_monthly_routes(q="Crosstown", limit=25)

    assert {route["route_id"] for route in result["routes"]} == {"A-10", "B2"}
    route = next(route for route in result["routes"] if route["route_id"] == "A-10")
    assert route["route_name"] == "North Connector"
    assert route["historical_route_names"] == ["Crosstown", "North Connector"]


def test_route_history_is_chronological_and_reports_missing_month(populated_routes):
    result = populated_routes.get_monthly_route_history("A-10")

    assert result["route_id"] == "A-10"
    assert result["first_month"] == "2024-01-01"
    assert result["last_month"] == "2024-03-01"
    assert result["observed_months"] == 2
    assert result["missing_months"] == 1
    assert [record["month_start"] for record in result["records"]] == ["2024-01-01", "2024-03-01"]
    assert [record["boardings"] for record in result["records"]] == ["10", "30"]
    assert "not unique passengers" in result["boardings_description"]


def test_route_history_preserves_name_per_observation(populated_routes):
    result = populated_routes.get_monthly_route_history("A-10")

    assert [record["route_name"] for record in result["records"]] == ["Crosstown", "North Connector"]
    assert result["historical_route_names"] == ["Crosstown", "North Connector"]


def test_unknown_route_returns_not_found(populated_routes):
    with pytest.raises(HTTPException) as error:
        populated_routes.get_monthly_route_history("UNKNOWN")

    assert error.value.status_code == 404


def test_empty_store_returns_empty_search_and_history_not_found(route_api):
    app_module, _ = route_api

    result = app_module.list_monthly_routes(q=None, limit=25)

    assert result == {"query": None, "count": 0, "total_matches": 0, "limit": 25, "routes": []}
    with pytest.raises(HTTPException) as error:
        app_module.get_monthly_route_history("A-10")
    assert error.value.status_code == 404


def test_invalid_query_parameter_bounds_are_published_in_openapi(populated_routes):
    app_module = populated_routes
    parameters = app_module.app.openapi()["paths"]["/api/monthly-data/routes"]["get"]["parameters"]
    schemas = {parameter["name"]: parameter["schema"] for parameter in parameters}

    query_schema = schemas["q"]
    if "anyOf" in query_schema:
        query_schema = next(option for option in query_schema["anyOf"] if option.get("type") == "string")
    assert query_schema["maxLength"] == 100
    assert schemas["limit"]["minimum"] == 1
    assert schemas["limit"]["maximum"] == 100


def test_endpoints_require_the_existing_bearer_authorization(route_api):
    app_module, _ = route_api

    with pytest.raises(HTTPException) as missing:
        app_module.authorize(None)
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException) as incorrect:
        app_module.authorize("Bearer wrong-key")
    assert incorrect.value.status_code == 401
    app_module.authorize("Bearer route-test-key")

    protected_paths = {
        route.path: route
        for route in app_module.app.routes
        if getattr(route, "path", None) in {
            "/api/monthly-data/routes",
            "/api/monthly-data/routes/{route_id}/history",
        }
    }
    assert set(protected_paths) == {
        "/api/monthly-data/routes",
        "/api/monthly-data/routes/{route_id}/history",
    }
    for route in protected_paths.values():
        assert any(dependency.call is app_module.authorize for dependency in route.dependant.dependencies)


def test_search_results_have_stable_sort_and_respect_limit(populated_routes):
    result = populated_routes.list_monthly_routes(q=None, limit=1)

    assert result["count"] == 1
    assert result["total_matches"] == 2
    assert result["routes"][0]["route_id"] == "A-10"
