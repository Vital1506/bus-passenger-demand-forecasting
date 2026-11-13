"""Small, invented fixtures for the isolated monthly import contract."""

from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
from io import BytesIO

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from src.monthly_data import MonthlyDataError, MonthlyRidershipStore, MonthlyStoreError, parse_monthly_csv


HEADERS = "Month_Beginning,route,routename,MonthTotal"


def csv_bytes(*records: tuple[str, str, str, str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADERS.split(","))
    writer.writerows(records)
    return output.getvalue().encode("utf-8")


def read_observations(path):
    return [
        (row["month_start"], row["route_id"], row["route_name"], row["boardings"])
        for row in MonthlyRidershipStore(path).observations()
    ]


def test_valid_csv_maps_to_monthly_contract_and_records_audit(tmp_path):
    contents = csv_bytes(("01/01/2024", "001A", "Route A", "1,234"))
    store = MonthlyRidershipStore(tmp_path / "monthly" / "cta.json")

    event = store.import_csv(contents, "cta_monthly.csv", "official portal export")

    assert read_observations(store.path) == [("2024-01-01", "001A", "Route A", "1234")]
    assert event["original_filename"] == "cta_monthly.csv"
    assert event["source_info"] == "official portal export"
    assert event["sha256"] == hashlib.sha256(contents).hexdigest()
    assert (event["incoming_records"], event["added_records"], event["updated_records"], event["unchanged_records"]) == (1, 1, 0, 0)
    assert event["final_record_count"] == 1


def test_source_headers_are_required():
    with pytest.raises(MonthlyDataError, match="missing required headers"):
        parse_monthly_csv(b"month,route_id,boardings\n2024-01-01,A,3\n")


@pytest.mark.parametrize("date", ["not-a-date", "02/02/2024", "2024-13-01"])
def test_invalid_or_non_month_start_dates_are_rejected(date):
    with pytest.raises(MonthlyDataError, match="Month_Beginning"):
        parse_monthly_csv(csv_bytes((date, "A", "Route A", "10")))


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "not-numeric", "1,00"])
def test_negative_nonfinite_or_nonnumeric_boardings_are_rejected(value):
    with pytest.raises(MonthlyDataError, match="MonthTotal"):
        parse_monthly_csv(csv_bytes(("01/01/2024", "A", "Route A", value)))


def test_empty_route_id_is_rejected():
    with pytest.raises(MonthlyDataError, match="route must not be blank"):
        parse_monthly_csv(csv_bytes(("01/01/2024", " ", "Route A", "10")))


def test_duplicate_route_month_keys_in_one_file_are_rejected():
    data = csv_bytes(("01/01/2024", "A", "Route A", "10"), ("2024-01-01", "A", "Another name", "11"))
    with pytest.raises(MonthlyDataError, match="duplicate route-month key"):
        parse_monthly_csv(data)


def test_malformed_csv_is_rejected():
    with pytest.raises(MonthlyDataError, match="Malformed CSV"):
        parse_monthly_csv((HEADERS + '\n01/01/2024,A,"unterminated,10').encode())


def test_absent_months_remain_absent_and_route_names_are_descriptive_history(tmp_path):
    store = MonthlyRidershipStore(tmp_path / "monthly.sqlite3")
    store.import_csv(
        csv_bytes(
            ("01/01/2024", "A", "Old Route Name", "10"),
            ("03/01/2024", "A", "New Route Name", "15"),
        ),
        "fixture.csv",
    )

    rows = read_observations(store.path)

    assert rows == [
        ("2024-01-01", "A", "Old Route Name", "10"),
        ("2024-03-01", "A", "New Route Name", "15"),
    ]
    assert all(row[0] != "2024-02-01" for row in rows)


def test_reupload_is_idempotent_and_does_not_duplicate_rows(tmp_path):
    store = MonthlyRidershipStore(tmp_path / "monthly.sqlite3")
    data = csv_bytes(("01/01/2024", "A", "Route A", "10"))

    store.import_csv(data, "same.csv")
    second = store.import_csv(data, "same.csv")

    assert second["added_records"] == 0
    assert second["updated_records"] == 0
    assert second["unchanged_records"] == 1
    assert second["final_record_count"] == 1
    assert len(read_observations(store.path)) == 1


def test_new_upload_merges_without_deleting_historical_records(tmp_path):
    store = MonthlyRidershipStore(tmp_path / "monthly.sqlite3")
    store.import_csv(csv_bytes(("01/01/2024", "A", "Route A", "10")), "jan.csv")

    result = store.import_csv(csv_bytes(("03/01/2024", "A", "Route A", "14")), "mar.csv")

    assert result["final_record_count"] == 2
    assert [row[0] for row in read_observations(store.path)] == ["2024-01-01", "2024-03-01"]


def test_corrected_existing_values_update_and_are_audited(tmp_path):
    store = MonthlyRidershipStore(tmp_path / "monthly.sqlite3")
    store.import_csv(csv_bytes(("01/01/2024", "A", "Route A", "10")), "initial.csv")

    result = store.import_csv(csv_bytes(("01/01/2024", "A", "Renamed Route A", "12")), "correction.csv")

    assert result["updated_records"] == 1
    assert result["added_records"] == 0
    assert result["final_record_count"] == 1
    assert read_observations(store.path) == [("2024-01-01", "A", "Renamed Route A", "12")]
    audits = json.loads(store.path.read_text(encoding="utf-8"))["imports"]
    assert [event["updated_records"] for event in audits] == [0, 1]


def test_failed_validation_or_atomic_replace_preserves_previous_data(tmp_path, monkeypatch):
    store = MonthlyRidershipStore(tmp_path / "monthly.json")
    initial = csv_bytes(("01/01/2024", "A", "Route A", "10"))
    store.import_csv(initial, "initial.csv")
    before = read_observations(store.path)

    with pytest.raises(MonthlyDataError):
        store.import_csv(csv_bytes(("02/02/2024", "A", "Bad month", "20")), "invalid.csv")
    assert read_observations(store.path) == before

    before_bytes = store.path.read_bytes()
    from src import monthly_data

    def fail_replace(_source, _destination):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(monthly_data.os, "replace", fail_replace)
    with pytest.raises(MonthlyStoreError, match="previous data remains active"):
        store.import_csv(csv_bytes(("01/01/2024", "A", "Corrected", "99")), "blocked.csv")
    assert read_observations(store.path) == before
    assert store.path.read_bytes() == before_bytes
    assert not list(tmp_path.glob(".*.tmp"))


def test_status_for_missing_store_is_read_only(tmp_path):
    path = tmp_path / "monthly" / "not-created.json"
    result = MonthlyRidershipStore(path).status()

    assert result["record_count"] == 0
    assert result["last_import"] is None
    assert not path.exists()


@pytest.fixture
def api_context(monkeypatch, tmp_path):
    # Import the app with its normal application data redirected outside the repo.
    monkeypatch.setenv("BUS_FORECAST_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("BUS_FORECAST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BUS_FORECAST_API_KEY", "unit-test-key")
    app_module = importlib.import_module("src.app")
    monthly_path = tmp_path / "app-data" / "monthly" / "cta_monthly_ridership.json"
    monkeypatch.setattr(app_module, "MONTHLY_DATA_PATH", monthly_path)
    return app_module.app, monthly_path


def test_monthly_api_requires_auth_and_returns_expected_handler_shapes(api_context, monkeypatch):
    app, path = api_context
    app_module = importlib.import_module("src.app")
    contents = csv_bytes(("01/01/2024", "A1", "Fixture Route", "42"))

    with pytest.raises(HTTPException) as unauthorized:
        app_module.authorize(None)
    assert unauthorized.value.status_code == 401
    assert unauthorized.value.detail == "Enter the API access key in app settings"
    assert not path.exists()

    app_module.authorize("Bearer unit-test-key")
    uploaded_file = UploadFile(
        file=BytesIO(contents), filename="fixture.csv", headers=Headers({"content-type": "text/csv"})
    )
    uploaded = app_module.upload_monthly_data(file=uploaded_file, source_info="small invented test fixture")
    assert uploaded["import"]["added_records"] == 1
    assert uploaded["import"]["incoming_records"] == 1
    assert uploaded["status"]["granularity"] == "monthly"
    assert "storage_path" not in uploaded["status"]
    assert "model" not in uploaded
    assert "No model was trained" in uploaded["message"]

    status_body = app_module.get_monthly_data_status()
    assert status_body["record_count"] == 1
    assert status_body["last_import"]["original_filename"] == "fixture.csv"
    assert "storage_path" not in status_body

    for path_name in ("/api/monthly-data/upload", "/api/monthly-data/status"):
        route = next(route for route in app.routes if getattr(route, "path", None) == path_name)
        assert any(dependency.call is app_module.authorize for dependency in route.dependant.dependencies)


def test_monthly_status_requires_auth_and_does_not_create_store(api_context):
    _, path = api_context
    app_module = importlib.import_module("src.app")
    with pytest.raises(HTTPException) as unauthorized:
        app_module.authorize(None)
    assert unauthorized.value.status_code == 401
    assert unauthorized.value.detail == "Enter the API access key in app settings"
    assert not path.exists()

