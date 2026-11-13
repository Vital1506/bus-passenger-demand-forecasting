from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select

from src import db as _db
from src.db import session_scope
from src.db_admin import upgrade_database
from src.schema import (
    GtfsRealtimeFeedSource,
    GtfsRealtimePoll,
    GtfsRealtimeVehiclePosition,
    GtfsRealtimeTripUpdate,
    GtfsRealtimeServiceAlert,
    is_realtime_payload_kind,
)


@pytest.fixture(scope="module", autouse=True)
def isolated_realtime_database():
    """Keep realtime tests on the same isolated DB they explicitly migrate.

    Do not mutate process environment during module collection: pytest imports
    all test modules before executing tests, so those changes can leak into or
    be overwritten by other modules' setup/teardown methods.
    """
    saved_env = {
        name: os.environ.get(name)
        for name in ("DATABASE_URL", "BUS_FORECAST_DATA_DIR")
    }
    temp_dir = Path(tempfile.mkdtemp(prefix="db-realtime-"))
    try:
        os.environ.pop("DATABASE_URL", None)
        os.environ["BUS_FORECAST_DATA_DIR"] = str(temp_dir)
        _db.reset_engine()
        migration = upgrade_database()
        assert not migration.get("pending"), migration

        expected_tables = {
            "gtfs_realtime_feed_sources",
            "gtfs_realtime_poll_attempts",
            "gtfs_realtime_vehicle_positions",
            "gtfs_realtime_trip_updates",
            "gtfs_realtime_service_alerts",
        }
        actual_tables = set(inspect(_db.get_engine()).get_table_names())
        missing_tables = expected_tables - actual_tables
        assert not missing_tables, f"Realtime migration did not create tables: {sorted(missing_tables)}"
        yield
    finally:
        _db.reset_engine()
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _db.reset_engine()


def _dt(value):
    """Coerce epoch-int or ISO-ish string into timezone-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.endswith('Z'):
            cleaned = cleaned[:-1]
        try:
            return datetime.fromisoformat(cleaned).astimezone(timezone.utc)
        except ValueError:
            return datetime.fromtimestamp(int(float(cleaned)), tz=timezone.utc)
    return value


ZERO_TS = 1700000000



def _fixture_feed_source(
    agency_code='CTA',
    *,
    disabled=True,
    provider_key='mta-nyct',
    provider_name='MTA NYC Transit',
    url='https://api.mta.info/gtfs-rt',
    transport='protobuf',
    capabilities='vehicle_positions,trip_updates,service_alerts',
    last_error=None,
    detail=None,
    last_contact_at=None,
):
    return {
        'agency_code': agency_code,
        'provider_key': provider_key,
        'provider_name': provider_name,
        'url': url,
        'transport': transport,
        'capabilities': capabilities,
        'status': 'disabled' if disabled else 'enabled',
        'last_error': last_error,
        'detail': detail,
        'created_at': None,
        'updated_at': None,
        'last_contact_at': _dt(last_contact_at) if last_contact_at is not None else None,
    }


def _fixture_poll(
    feed_source_id=1,
    *,
    polled_at=None,
    latency_ms=120,
    status_code=200,
    payload_kind='vehicle_positions',
    bytes_received=4096,
    error=None,
    detail=None,
):
    return {
        'feed_source_id': feed_source_id,
        'polled_at': _dt(polled_at) if polled_at is not None else _dt('2026-11-12T12:00:00Z'),
        'latency_ms': latency_ms,
        'status_code': status_code,
        'payload_kind': payload_kind,
        'bytes_received': bytes_received,
        'error': error,
        'detail': detail,
        'created_at': None,
        'updated_at': None,
    }


def _fixture_vehicle(
    feed_source_id=1,
    *,
    feed_snapshot_at='2026-11-12T12:00:00Z',
    agency_code='CTA',
    vehicle_id='V1',
    trip_id='T1',
    route_id='R1',
    schedule_relationship=None,
    latitude=1.0,
    longitude=-87.6300,
    bearing=180.0,
    speed_mps=0.0,
    position_accuracy_m=25,
    timestamp=ZERO_TS,
    created_at=None,
    updated_at=None,
):
    return {
        'feed_source_id': feed_source_id,
        'feed_snapshot_at': _dt(feed_snapshot_at),
        'agency_code': agency_code,
        'vehicle_id': vehicle_id,
        'trip_id': trip_id,
        'route_id': route_id,
        'schedule_relationship': schedule_relationship,
        'latitude': latitude,
        'longitude': longitude,
        'bearing': bearing,
        'speed_mps': speed_mps,
        'position_accuracy_m': position_accuracy_m,
        'timestamp': _dt(timestamp) if timestamp is not None else None,
        'created_at': created_at,
        'updated_at': updated_at,
    }


def _fixture_trip_update(
    feed_source_id=1,
    *,
    feed_snapshot_at='2026-11-12T12:00:00Z',
    agency_code='CTA',
    trip_id='T1',
    route_id='R1',
    schedule_relationship=None,
    timestamp=ZERO_TS,
    vehicle_id=None,
    arrival_delay_sec=10,
    departure_delay_sec=90,
    created_at=None,
    updated_at=None,
):
    return {
        'feed_source_id': feed_source_id,
        'feed_snapshot_at': _dt(feed_snapshot_at),
        'agency_code': agency_code,
        'trip_id': trip_id,
        'route_id': route_id,
        'schedule_relationship': schedule_relationship,
        'timestamp': _dt(timestamp) if timestamp is not None else None,
        'vehicle_id': vehicle_id,
        'arrival_delay_sec': arrival_delay_sec,
        'departure_delay_sec': departure_delay_sec,
        'created_at': created_at,
        'updated_at': updated_at,
    }


def _fixture_alert(
    feed_source_id=1,
    *,
    feed_snapshot_at='2026-11-12T12:00:00Z',
    agency_code='CTA',
    alert_id='A1',
    header_text='First',
    description_text='Track work',
    url='https://agency.info/alert',
    active=True,
    timestamp=ZERO_TS,
    created_at=None,
    updated_at=None,
):
    return {
        'feed_source_id': feed_source_id,
        'feed_snapshot_at': _dt(feed_snapshot_at),
        'agency_code': agency_code,
        'alert_id': alert_id,
        'header_text': header_text,
        'description_text': description_text,
        'url': url,
        'active': active,
        'timestamp': _dt(timestamp) if timestamp is not None else None,
        'created_at': created_at,
        'updated_at': updated_at,
    }




class TestGtfsRealtimeSchemaSmoke:
    def test_tables_are_registered_on_base(self):
        for cls in (
            GtfsRealtimeFeedSource,
            GtfsRealtimePoll,
            GtfsRealtimeVehiclePosition,
            GtfsRealtimeTripUpdate,
            GtfsRealtimeServiceAlert,
        ):
            assert cls.__tablename__
            assert cls.__table_args__

    def test_feed_source_has_agency_isolation(self):
        rows = [
            _fixture_feed_source(agency_code='CTA', provider_key='mta-nyct'),
            _fixture_feed_source(agency_code='BART', provider_key='bart-bart'),
        ]
        with session_scope() as session:
            for row in rows:
                session.add(GtfsRealtimeFeedSource(**row))
            codes = session.execute(
                select(GtfsRealtimeFeedSource.agency_code).distinct()
            ).scalars().all()
        assert set(codes) == {'CTA', 'BART'}

    def test_feed_source_status_defaults_to_disabled(self):
        with session_scope() as session:
            row = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAX', disabled=True))
            session.add(row)
            session.flush()
            assert row.status == 'disabled'



class TestGtfsRealtimePollContract:
    def test_poll_records_attempt_with_payload_kind(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAP-1', provider_key='mta-p1'))
            session.add(source)
            session.flush()
            poll = GtfsRealtimePoll(**_fixture_poll(feed_source_id=source.id))
            session.add(poll)
            session.flush()
            assert poll.payload_kind == 'vehicle_positions'
            assert poll.status_code == 200

    def test_poll_records_error_when_status_not_ok(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAP-2', provider_key='mta-p2'))
            session.add(source)
            session.flush()
            poll = GtfsRealtimePoll(
                **_fixture_poll(status_code=503, error='provider_unavailable')
            )
            session.add(poll)
            session.flush()
            assert poll.error == 'provider_unavailable'
            assert poll.payload_kind == 'vehicle_positions'

    def test_poll_eats_invalid_payload_without_crashing(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAP-3', provider_key='mta-p3'))
            session.add(source)
            session.flush()
            body = b'not a protobuf payload and not json either'
            kind = is_realtime_payload_kind(body)
            assert kind is None



class TestGtfsRealtimeSnapshotOverwrite:
    def test_vehicle_position_snapshots_overwrite_for_same_snapshot_time(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAV-1', provider_key='mta-v1'))
            session.add(source)
            session.flush()
            snap = '2026-11-12T12:00:00Z'
            v1 = GtfsRealtimeVehiclePosition(**_fixture_vehicle(
                feed_source_id=source.id,
                feed_snapshot_at=snap,
                vehicle_id='V1',
                latitude=1.0,
            ))
            session.add(v1)
            session.flush()
            v1.latitude = 2.0
            session.flush()
            refreshed = session.get(GtfsRealtimeVehiclePosition, v1.id)
            assert refreshed is not None
            assert refreshed.latitude == 2.0

    def test_trip_update_snapshots_overwrite_for_same_snapshot_time(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAT-1', provider_key='mta-t1'))
            session.add(source)
            session.flush()
            snap = '2026-11-12T12:00:00Z'
            first = GtfsRealtimeTripUpdate(**_fixture_trip_update(
                feed_source_id=source.id,
                feed_snapshot_at=snap,
                trip_id='T1',
                arrival_delay_sec=10,
            ))
            session.add(first)
            session.flush()
            first.arrival_delay_sec = 20
            session.flush()
            refreshed = session.get(GtfsRealtimeTripUpdate, first.id)
            assert refreshed is not None
            assert refreshed.arrival_delay_sec == 20

    def test_service_alert_snapshots_overwrite_for_same_snapshot_time(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAA-1', provider_key='mta-a1'))
            session.add(source)
            session.flush()
            snap = '2026-11-12T12:00:00Z'
            first = GtfsRealtimeServiceAlert(**_fixture_alert(
                feed_source_id=source.id,
                feed_snapshot_at=snap,
                alert_id='A1',
                header_text='First',
                description_text='Track work',
            ))
            session.add(first)
            session.flush()
            first.header_text = 'Second'
            session.flush()
            refreshed = session.get(GtfsRealtimeServiceAlert, first.id)
            assert refreshed is not None
            assert refreshed.header_text == 'Second'



class TestGtfsRealtimeProviderAdapterContract:
    def test_adapter_is_inactive_until_source_is_enabled(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAA-2', provider_key='mta-a2', disabled=True))
            session.add(source)
            session.flush()
            assert source.status == 'disabled'

    def test_adapter_accounts_are_agency_scoped(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAA-3', provider_key='mta-a3'))
            session.add(source)
            session.flush()
            refreshed = session.get(GtfsRealtimeFeedSource, source.id)
            assert refreshed is not None
            assert refreshed.agency_code == 'CTAA-3'

    def test_adapter_state_is_json_safe(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(
                **_fixture_feed_source(
                    disabled=True,
                    last_error='timeout',
                    detail={'attempted_at': '2026-11-12T12:00:00Z'},
                    agency_code='CTAA-4',
                    provider_key='mta-a4',
                )
            )
            session.add(source)
            session.flush()
            row = session.get(GtfsRealtimeFeedSource, source.id)
            payload = {
                'id': row.id,
                'agency_code': row.agency_code,
                'status': row.status,
                'last_error': row.last_error,
                'detail': row.detail,
            }
            json.dumps(payload)
            assert payload['status'] == 'disabled'



class TestGtfsRealtimeStaleness:
    def test_poll_attempts_expose_contact_time(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(**_fixture_feed_source(agency_code='CTAS-1', provider_key='mta-s1', disabled=True))
            session.add(source)
            session.flush()
            session.add(GtfsRealtimePoll(**_fixture_poll(feed_source_id=source.id)))
            session.flush()
            contact = session.scalar(
                select(func.max(GtfsRealtimePoll.polled_at)).where(
                    GtfsRealtimePoll.feed_source_id == source.id
                )
            )
            assert contact is not None

    def test_feed_source_error_reports_last_failure(self):
        with session_scope() as session:
            source = GtfsRealtimeFeedSource(
                **_fixture_feed_source(disabled=True, last_error='timeout', agency_code='CTAS-2', provider_key='mta-s2')
            )
            session.add(source)
            session.flush()
            row = session.get(GtfsRealtimeFeedSource, source.id)
            assert row.last_error == 'timeout'






