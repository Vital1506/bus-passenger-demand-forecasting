"""Comprehensive unit and integration tests for Phase 2 GTFS Schedule Integration."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.models import Base
from src.db.session import init_db
from src.gtfs_importer import (
    GTFSValidationError,
    import_gtfs_feed,
    parse_and_validate_gtfs,
)
from src.gtfs_service import (
    get_route_details,
    list_agencies,
    list_feeds,
    list_routes,
    list_stops,
)


def create_sample_gtfs_zip(
    corrupt_file: str | None = None,
    omit_file: str | None = None,
    bad_coords: bool = False,
    bad_fk: bool = False,
    bad_date: bool = False,
    zip_slip: bool = False,
) -> bytes:
    """Helper to generate in-memory valid or invalid GTFS ZIP archives."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if omit_file != "agency.txt":
            zf.writestr(
                "agency.txt",
                "agency_id,agency_name,agency_url,agency_timezone,agency_lang\n"
                "CTA,Chicago Transit Authority,http://transitchicago.com,America/Chicago,en\n",
            )

        if omit_file != "routes.txt":
            zf.writestr(
                "routes.txt",
                "route_id,agency_id,route_short_name,route_long_name,route_type,route_color\n"
                "1,CTA,1,Bronzeville / Union Station,3,008080\n"
                "2,CTA,2,Hyde Park Express,3,FF6600\n",
            )

        if omit_file != "stops.txt":
            lat = 95.0 if bad_coords else 41.881832
            zf.writestr(
                "stops.txt",
                "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
                f"S1,1001,Union Station,{lat},-87.640479\n"
                "S2,1002,State & Madison,41.881944,-87.627778\n"
                "S3,1003,Museum Campus,41.866111,-87.616944\n",
            )

        if omit_file != "calendar.txt":
            start_d = "not-a-date" if bad_date else "20240101"
            zf.writestr(
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                f"WK,1,1,1,1,1,0,0,{start_d},20241231\n"
                "SAT,0,0,0,0,0,1,0,20240101,20241231\n",
            )

        if omit_file != "calendar_dates.txt":
            zf.writestr(
                "calendar_dates.txt",
                "service_id,date,exception_type\n"
                "WK,20240704,2\n",
            )

        if omit_file != "trips.txt":
            r_id = "UNKNOWN_ROUTE" if bad_fk else "1"
            zf.writestr(
                "trips.txt",
                "route_id,service_id,trip_id,trip_headsign,direction_id,shape_id\n"
                f"{r_id},WK,T101,Eastbound,0,SH1\n"
                "1,WK,T102,Eastbound,0,SH1\n"
                "2,WK,T201,Southbound,1,SH2\n",
            )

        if omit_file != "stop_times.txt":
            zf.writestr(
                "stop_times.txt",
                "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n"
                "T101,S1,1,08:00:00,08:00:00\n"
                "T101,S2,2,08:15:00,08:16:00\n"
                "T101,S3,3,08:35:00,08:35:00\n"
                "T102,S1,1,09:00:00,09:00:00\n"
                "T102,S2,2,09:15:00,09:16:00\n"
                "T102,S3,3,09:35:00,09:35:00\n"
                "T201,S1,1,07:30:00,07:30:00\n"
                "T201,S3,2,07:50:00,07:50:00\n",
            )

        zf.writestr(
            "shapes.txt",
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
            "SH1,41.8818,-87.6404,1\n"
            "SH1,41.8819,-87.6277,2\n"
            "SH1,41.8661,-87.6169,3\n",
        )

        if zip_slip:
            zf.writestr("../../malicious.txt", "evil")

    return buf.getvalue()


@pytest.fixture
def gtfs_db():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        yield session


def test_parse_and_validate_valid_gtfs():
    zip_bytes = create_sample_gtfs_zip()
    payload, summary = parse_and_validate_gtfs(zip_bytes, feed_id="test_feed")

    assert summary.feed_id == "test_feed"
    assert summary.agency_id == "CTA"
    assert summary.agencies_count == 1
    assert summary.routes_count == 2
    assert summary.stops_count == 3
    assert summary.trips_count == 3
    assert summary.stop_times_count == 8
    assert summary.calendar_count == 2
    assert summary.shapes_count == 3
    assert len(summary.sha256) == 64


def test_gtfs_missing_required_files():
    for req in ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt"]:
        bad_zip = create_sample_gtfs_zip(omit_file=req)
        with pytest.raises(GTFSValidationError, match=f"Missing required GTFS files:.*{req}"):
            parse_and_validate_gtfs(bad_zip)


def test_gtfs_invalid_coordinates():
    bad_zip = create_sample_gtfs_zip(bad_coords=True)
    with pytest.raises(GTFSValidationError, match="coordinates out of range"):
        parse_and_validate_gtfs(bad_zip)


def test_gtfs_bad_foreign_key_relationship():
    bad_zip = create_sample_gtfs_zip(bad_fk=True)
    with pytest.raises(GTFSValidationError, match="references unknown route_id"):
        parse_and_validate_gtfs(bad_zip)


def test_gtfs_bad_date_format():
    bad_zip = create_sample_gtfs_zip(bad_date=True)
    with pytest.raises(GTFSValidationError, match="YYYYMMDD format"):
        parse_and_validate_gtfs(bad_zip)


def test_gtfs_zip_slip_rejection():
    bad_zip = create_sample_gtfs_zip(zip_slip=True)
    with pytest.raises(GTFSValidationError, match="Insecure path detected in ZIP"):
        parse_and_validate_gtfs(bad_zip)


def test_import_and_query_gtfs(gtfs_db):
    zip_bytes = create_sample_gtfs_zip()
    summary = import_gtfs_feed(gtfs_db, zip_bytes, feed_id="feed_cta_01", agency_id="CTA")
    gtfs_db.commit()

    assert summary.feed_id == "feed_cta_01"

    # 1. Feeds list
    feeds = list_feeds(gtfs_db)
    assert len(feeds) == 1
    assert feeds[0]["feed_id"] == "feed_cta_01"
    assert feeds[0]["is_active"] is True

    # 2. Agencies list
    agencies = list_agencies(gtfs_db)
    assert len(agencies) == 1
    assert agencies[0]["agency_name"] == "Chicago Transit Authority"

    # 3. Routes list
    routes = list_routes(gtfs_db)
    assert len(routes) == 2
    r1 = next(r for r in routes if r["route_id"] == "1")
    assert r1["route_short_name"] == "1"
    assert r1["trips_count"] == 2

    # Query search
    filtered = list_routes(gtfs_db, query="Hyde Park")
    assert len(filtered) == 1
    assert filtered[0]["route_id"] == "2"

    # 4. Route details
    details = get_route_details(gtfs_db, "1")
    assert details is not None
    assert details["route_id"] == "1"
    assert details["first_service_time"] == "08:00:00"
    assert details["last_service_time"] == "09:35:00"
    assert details["trips_count"] == 2
    assert len(details["stops"]) == 3
    assert details["stops"][0]["stop_name"] == "Union Station"
    assert details["stops"][1]["stop_name"] == "State & Madison"
    assert details["stops"][2]["stop_name"] == "Museum Campus"
    assert details["has_shape"] is True
    assert len(details["shape_points"]) == 3

    # Calendar details
    assert len(details["calendar"]) == 1
    assert "Mon" in details["calendar"][0]["days"]
    assert "Fri" in details["calendar"][0]["days"]
    assert "Sat" not in details["calendar"][0]["days"]

    # Calendar exception dates
    assert len(details["calendar_exceptions"]) == 1
    assert details["calendar_exceptions"][0]["date"] == "2024-07-04"
    assert details["calendar_exceptions"][0]["exception_type"] == "removed"

    # 5. Stops list
    stops = list_stops(gtfs_db, query="Madison")
    assert len(stops) == 1
    assert stops[0]["stop_name"] == "State & Madison"
    assert stops[0]["stop_lat"] == 41.881944


def test_gtfs_endpoints_and_handlers(tmp_path, monkeypatch):
    import io
    from starlette.datastructures import UploadFile
    from fastapi import HTTPException
    import src.app as app_mod

    db_path = tmp_path / "test_gtfs_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("BUS_FORECAST_DATA_DIR", str(tmp_path / "gtfs_data"))
    app_mod.init_db()

    # 1. Check routes registered in FastAPI
    route_paths = {route.path for route in app_mod.app.routes}
    assert "/api/gtfs/upload" in route_paths
    assert "/api/gtfs/feeds" in route_paths
    assert "/api/gtfs/agencies" in route_paths
    assert "/api/gtfs/routes" in route_paths
    assert "/api/gtfs/routes/{route_id}" in route_paths
    assert "/api/gtfs/stops" in route_paths

    # 2. Upload invalid extension
    bad_upload = UploadFile(file=io.BytesIO(b"not a zip"), filename="invalid.txt")
    with pytest.raises(HTTPException) as exc_info:
        app_mod.upload_gtfs_feed(file=bad_upload)
    assert exc_info.value.status_code == 400

    # 3. Upload valid GTFS ZIP
    zip_bytes = create_sample_gtfs_zip()
    good_upload = UploadFile(file=io.BytesIO(zip_bytes), filename="gtfs_cta.zip")
    res = app_mod.upload_gtfs_feed(file=good_upload, feed_id="api_feed_01", agency_id="CTA")
    assert res["message"] == "GTFS feed imported successfully"
    assert res["summary"]["feed_id"] == "api_feed_01"
    assert res["summary"]["counts"]["routes"] == 2

    # 4. Feeds endpoint
    feeds = app_mod.get_gtfs_feeds()
    assert len(feeds) == 1
    assert feeds[0]["feed_id"] == "api_feed_01"

    # 5. Agencies endpoint
    agencies = app_mod.get_gtfs_agencies()
    assert len(agencies) == 1
    assert agencies[0]["agency_name"] == "Chicago Transit Authority"

    # 6. Routes endpoint
    routes = app_mod.get_gtfs_routes()
    assert len(routes) == 2
    r_search = app_mod.get_gtfs_routes(search="Hyde")
    assert len(r_search) == 1
    assert r_search[0]["route_id"] == "2"

    # 7. Route details endpoint
    details = app_mod.get_gtfs_route_details("1")
    assert details["route_id"] == "1"
    assert len(details["stops"]) == 3
    assert len(details["shape_points"]) == 3

    # Route details not found
    with pytest.raises(HTTPException) as exc_404:
        app_mod.get_gtfs_route_details("NONEXISTENT")
    assert exc_404.value.status_code == 404

    # 8. Stops endpoint
    stops = app_mod.get_gtfs_stops(search="Union")
    assert len(stops) == 1
    assert stops[0]["stop_name"] == "Union Station"

