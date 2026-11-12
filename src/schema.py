"""SQLAlchemy schema for the transit intelligence platform.

The database is the authoritative store for operator data. The legacy CSV and
JSON files remain supported as import/export formats and as rollback copies
during the one-time migration in :mod:`src.migrate_to_db`.

Only observed data is stored. Synthetic demonstration rows are marked with
``provenance='synthetic'`` and are never mixed with real observations in a
query that reports operational figures.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Agency(Base, TimestampMixin):
    __tablename__ = "agencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(300))
    source: Mapped[str | None] = mapped_column(String(120))

    routes: Mapped[list["Route"]] = relationship(back_populates="agency", cascade="all, delete-orphan")
    stops: Mapped[list["Stop"]] = relationship(back_populates="agency", cascade="all, delete-orphan")


class Route(Base, TimestampMixin):
    __tablename__ = "routes"
    __table_args__ = (
        UniqueConstraint("agency_id", "route_id", name="uq_routes_agency_route"),
        Index("ix_routes_route_id", "route_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agency_id: Mapped[int | None] = mapped_column(ForeignKey("agencies.id", ondelete="CASCADE"), index=True)
    route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(120))
    long_name: Mapped[str | None] = mapped_column(String(300))
    route_type: Mapped[int | None] = mapped_column(Integer)

    agency: Mapped[Agency | None] = relationship(back_populates="routes")


class Stop(Base, TimestampMixin):
    __tablename__ = "stops"
    __table_args__ = (
        UniqueConstraint("agency_id", "stop_id", name="uq_stops_agency_stop"),
        Index("ix_stops_stop_id", "stop_id"),
        Index("ix_stops_coordinates", "latitude", "longitude"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agency_id: Mapped[int | None] = mapped_column(ForeignKey("agencies.id", ondelete="CASCADE"), index=True)
    stop_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    agency: Mapped[Agency | None] = relationship(back_populates="stops")


class DailyObservation(Base, TimestampMixin):
    """Observed daily boardings for one route and service date."""

    __tablename__ = "daily_observations"
    __table_args__ = (
        UniqueConstraint("route_id", "service_date", name="uq_daily_route_date"),
        Index("ix_daily_service_date", "service_date"),
        Index("ix_daily_route", "route_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[str] = mapped_column(String(128), nullable=False)
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    passengers: Mapped[float] = mapped_column(Float, nullable=False)
    source_system: Mapped[str | None] = mapped_column(String(80))
    provenance: Mapped[str] = mapped_column(String(32), default="observed", nullable=False)
    import_id: Mapped[int | None] = mapped_column(ForeignKey("feed_imports.id", ondelete="SET NULL"))


class MonthlyObservation(Base, TimestampMixin):
    """Observed monthly boardings for one route and month."""

    __tablename__ = "monthly_observations"
    __table_args__ = (
        UniqueConstraint("route_id", "month_start", name="uq_monthly_route_month"),
        Index("ix_monthly_month_start", "month_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    route_name: Mapped[str] = mapped_column(String(200), nullable=False)
    month_start: Mapped[date] = mapped_column(Date, nullable=False)
    boardings: Mapped[float] = mapped_column(Float, nullable=False)
    source_system: Mapped[str | None] = mapped_column(String(80))
    provenance: Mapped[str] = mapped_column(String(32), default="observed", nullable=False)
    import_id: Mapped[int | None] = mapped_column(ForeignKey("feed_imports.id", ondelete="SET NULL"))


class FeedImport(Base):
    """One import run: file checksum, counts, and recorded outcome."""

    __tablename__ = "feed_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(300), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditEvent(Base):
    """Aggregate-only audit trail; never stores passenger rows."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    source_system: Mapped[str | None] = mapped_column(String(80))
    agency_code: Mapped[str | None] = mapped_column(String(64))
    record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    route_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ModelVersion(Base):
    """Registered model artifact with metrics and lifecycle status."""

    __tablename__ = "model_versions"
    __table_args__ = (Index("ix_model_versions_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    trained_through: Mapped[date | None] = mapped_column(Date)
    horizon_days: Mapped[int | None] = mapped_column(Integer)
    route_count: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GtfsFeed(Base, TimestampMixin):
    """Imported GTFS Schedule feed version."""

    __tablename__ = "gtfs_feeds"
    __table_args__ = (Index("ix_gtfs_feeds_agency", "agency_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agency_code: Mapped[str] = mapped_column(String(64), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    file_name: Mapped[str] = mapped_column(String(300), nullable=False)
    feed_start: Mapped[date | None] = mapped_column(Date)
    feed_end: Mapped[date | None] = mapped_column(Date)
    route_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stop_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trip_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stop_time_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="imported", nullable=False)
    warnings: Mapped[dict | None] = mapped_column(JSON)


class GtfsRoute(Base):
    __tablename__ = "gtfs_routes"
    __table_args__ = (
        UniqueConstraint("feed_id", "route_id", name="uq_gtfs_routes_feed_route"),
        Index("ix_gtfs_routes_route_id", "route_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("gtfs_feeds.id", ondelete="CASCADE"), index=True)
    agency_code: Mapped[str] = mapped_column(String(64), nullable=False)
    route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(120))
    long_name: Mapped[str | None] = mapped_column(String(300))
    route_type: Mapped[int | None] = mapped_column(Integer)
    color: Mapped[str | None] = mapped_column(String(8))


class GtfsStop(Base):
    __tablename__ = "gtfs_stops"
    __table_args__ = (
        UniqueConstraint("feed_id", "stop_id", name="uq_gtfs_stops_feed_stop"),
        Index("ix_gtfs_stops_stop_id", "stop_id"),
        Index("ix_gtfs_stops_coordinates", "latitude", "longitude"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("gtfs_feeds.id", ondelete="CASCADE"), index=True)
    agency_code: Mapped[str] = mapped_column(String(64), nullable=False)
    stop_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    location_type: Mapped[int | None] = mapped_column(Integer)


class GtfsTrip(Base):
    __tablename__ = "gtfs_trips"
    __table_args__ = (
        UniqueConstraint("feed_id", "trip_id", name="uq_gtfs_trips_feed_trip"),
        Index("ix_gtfs_trips_route", "route_id"),
        Index("ix_gtfs_trips_service", "service_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("gtfs_feeds.id", ondelete="CASCADE"), index=True)
    agency_code: Mapped[str] = mapped_column(String(64), nullable=False)
    trip_id: Mapped[str] = mapped_column(String(128), nullable=False)
    route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    service_id: Mapped[str] = mapped_column(String(128), nullable=False)
    headsign: Mapped[str | None] = mapped_column(String(300))
    direction_id: Mapped[int | None] = mapped_column(Integer)
    shape_id: Mapped[str | None] = mapped_column(String(128))


class GtfsStopTime(Base):
    __tablename__ = "gtfs_stop_times"
    __table_args__ = (
        UniqueConstraint("feed_id", "trip_id", "stop_sequence", name="uq_gtfs_stop_times_feed_trip_sequence"),
        Index("ix_gtfs_stop_times_stop", "stop_id"),
        Index("ix_gtfs_stop_times_departure", "departure_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("gtfs_feeds.id", ondelete="CASCADE"), index=True)
    trip_id: Mapped[str] = mapped_column(String(128), nullable=False)
    stop_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stop_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    arrival_time: Mapped[str | None] = mapped_column(String(16))
    departure_time: Mapped[str | None] = mapped_column(String(16))


class GtfsCalendar(Base):
    __tablename__ = "gtfs_calendars"
    __table_args__ = (UniqueConstraint("feed_id", "service_id", name="uq_gtfs_calendars_feed_service"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("gtfs_feeds.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[str] = mapped_column(String(128), nullable=False)
    monday: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tuesday: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    wednesday: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    thursday: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    friday: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    saturday: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sunday: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)


class GtfsCalendarDate(Base):
    __tablename__ = "gtfs_calendar_dates"
    __table_args__ = (UniqueConstraint("feed_id", "service_id", "date", name="uq_gtfs_calendar_dates"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("gtfs_feeds.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[str] = mapped_column(String(128), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    exception_type: Mapped[int] = mapped_column(Integer, nullable=False)


class GtfsShapePoint(Base):
    __tablename__ = "gtfs_shape_points"
    __table_args__ = (UniqueConstraint("feed_id", "shape_id", "sequence", name="uq_gtfs_shape_points"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("gtfs_feeds.id", ondelete="CASCADE"), index=True)
    shape_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

class GtfsRealtimeFeedSource(Base, TimestampMixin):
    """One configured GTFS-Realtime provider endpoint.

    Providers are registered by operator, not invented by the platform.
    Until a provider is configured with real credentials it stays
    ``disabled`` and the adapter never attempts live requests.
    """

    __tablename__ = "gtfs_realtime_feed_sources"
    __table_args__ = (
        UniqueConstraint("agency_code", "provider_key", name="uq_gtfs_realtime_feed_sources"),
        Index("ix_gtfs_realtime_feed_sources_agency", "agency_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agency_code: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str | None] = mapped_column(String(600))
    transport: Mapped[str] = mapped_column(String(32), default="protobuf", nullable=False)
    capabilities: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="disabled", nullable=False)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(400))
    detail: Mapped[dict | None] = mapped_column(JSON)


class GtfsRealtimePoll(Base, TimestampMixin):
    """One poll attempt against a provider endpoint."""

    __tablename__ = "gtfs_realtime_poll_attempts"
    __table_args__ = (
        Index("ix_gtfs_realtime_poll_source", "feed_source_id"),
        Index("ix_gtfs_realtime_poll_at", "polled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_source_id: Mapped[int] = mapped_column(
        ForeignKey("gtfs_realtime_feed_sources.id", ondelete="CASCADE"), index=True
    )
    polled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status_code: Mapped[int | None] = mapped_column(Integer)
    payload_kind: Mapped[str | None] = mapped_column(String(32))
    bytes_received: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(400))
    detail: Mapped[dict | None] = mapped_column(JSON)


class GtfsRealtimeVehiclePosition(Base, TimestampMixin):
    """Snapshot of one vehicle's reported position.

    Rows are overwritten on each successful poll; the feed source and
    snapshot time are the real identity, not the DB row id.
    """

    __tablename__ = "gtfs_realtime_vehicle_positions"
    __table_args__ = (
        UniqueConstraint("feed_source_id", "feed_snapshot_at", "vehicle_id", name="uq_gtfs_realtime_vehicle_position"),
        Index("ix_gtfs_realtime_vehicle_source", "feed_source_id"),
        Index("ix_gtfs_realtime_vehicle_at", "feed_snapshot_at"),
        Index("ix_gtfs_realtime_vehicle_agency", "agency_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_source_id: Mapped[int] = mapped_column(
        ForeignKey("gtfs_realtime_feed_sources.id", ondelete="CASCADE"), index=True
    )
    feed_snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agency_code: Mapped[str] = mapped_column(String(64), nullable=False)
    vehicle_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trip_id: Mapped[str | None] = mapped_column(String(128))
    route_id: Mapped[str | None] = mapped_column(String(64))
    schedule_relationship: Mapped[int | None] = mapped_column(Integer)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    bearing: Mapped[float | None] = mapped_column(Float)
    speed_mps: Mapped[float | None] = mapped_column(Float)
    position_accuracy_m: Mapped[int | None] = mapped_column(Integer)
    timestamp: Mapped[int | None] = mapped_column(Integer)


class GtfsRealtimeTripUpdate(Base, TimestampMixin):
    """Snapshot of one trip's schedule deviation."""

    __tablename__ = "gtfs_realtime_trip_updates"
    __table_args__ = (
        UniqueConstraint("feed_source_id", "feed_snapshot_at", "trip_id", name="uq_gtfs_realtime_trip_update"),
        Index("ix_gtfs_realtime_trip_source", "feed_source_id"),
        Index("ix_gtfs_realtime_trip_at", "feed_snapshot_at"),
        Index("ix_gtfs_realtime_trip_agency", "agency_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_source_id: Mapped[int] = mapped_column(
        ForeignKey("gtfs_realtime_feed_sources.id", ondelete="CASCADE"), index=True
    )
    feed_snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agency_code: Mapped[str] = mapped_column(String(64), nullable=False)
    trip_id: Mapped[str] = mapped_column(String(128), nullable=False)
    route_id: Mapped[str | None] = mapped_column(String(64))
    schedule_relationship: Mapped[int | None] = mapped_column(Integer)
    timestamp: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[str | None] = mapped_column(String(128))
    arrival_delay_sec: Mapped[int | None] = mapped_column(Integer)
    departure_delay_sec: Mapped[int | None] = mapped_column(Integer)


class GtfsRealtimeServiceAlert(Base, TimestampMixin):
    """Snapshot of one service alert from a provider."""

    __tablename__ = "gtfs_realtime_service_alerts"
    __table_args__ = (
        UniqueConstraint("feed_source_id", "feed_snapshot_at", "alert_id", name="uq_gtfs_realtime_service_alert"),
        Index("ix_gtfs_realtime_alert_source", "feed_source_id"),
        Index("ix_gtfs_realtime_alert_at", "feed_snapshot_at"),
        Index("ix_gtfs_realtime_alert_agency", "agency_code"),
        Index("ix_gtfs_realtime_alert_active", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_source_id: Mapped[int] = mapped_column(
        ForeignKey("gtfs_realtime_feed_sources.id", ondelete="CASCADE"), index=True
    )
    feed_snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agency_code: Mapped[str] = mapped_column(String(64), nullable=False)
    alert_id: Mapped[str] = mapped_column(String(200), nullable=False)
    header_text: Mapped[str | None] = mapped_column(String(400))
    description_text: Mapped[str | None] = mapped_column(String(1200))
    url: Mapped[str | None] = mapped_column(String(600))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timestamp: Mapped[int | None] = mapped_column(Integer)

"""GTFS-realtime types deliberately reuse the same snapshot-identity pattern
as the static GTFS tables: each poll produces one snapshot, and repeated
snapshots overwrite earlier rows for the same (feed source, snapshot time,
entity id). That keeps live views cache-friendly and makes staleness
inspectable from the poll_attempts table.
"""

def is_realtime_payload_kind(raw: bytes | None) -> str | None:
    """Best-effort classification of a raw feed payload before parsing.

    Returns one of ``"vehicle_positions"``, ``"trip_updates"``,
    ``"service_alerts"``, or ``None``. This is used only for poll
    bookkeeping; it is not a schema validator and does not reject payloads.
    """
    if not raw:
        return None
    head = raw[:64].lower()
    if b"vehicle" in head and b"position" in head:
        return "vehicle_positions"
    if b"trip" in head and b"update" in head:
        return "trip_updates"
    if b"alert" in head:
        return "service_alerts"
    return None
