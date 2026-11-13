"""SQLAlchemy ORM models for Transit Intelligence V2.

Covers:
- Daily route passenger observations
- CTA / Operator monthly route boardings
- Source metadata and data operation audit events
- Monthly import audit logs
- Model artifact metadata
- GTFS Schedule data models (agency, routes, stops, trips, stop_times, calendar, calendar_dates, shapes)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Daily Passenger Observations
# ---------------------------------------------------------------------------
class DailyPassengerRecord(Base):
    __tablename__ = "daily_passenger_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default", index=True)
    service_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    route_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    passengers: Mapped[float] = mapped_column(Float, nullable=False)
    source_system: Mapped[str] = mapped_column(String(80), nullable=False, default="manual_csv")
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("agency_id", "service_date", "route_id", name="uq_daily_agency_date_route"),
        Index("ix_daily_agency_route_date", "agency_id", "route_id", "service_date"),
    )


# ---------------------------------------------------------------------------
# Monthly Passenger Observations
# ---------------------------------------------------------------------------
class MonthlyPassengerRecord(Base):
    __tablename__ = "monthly_passenger_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[str] = mapped_column(String(64), nullable=False, default="cta", index=True)
    month_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    route_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    route_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    boardings: Mapped[str] = mapped_column(String(64), nullable=False)  # Exact decimal string
    boardings_numeric: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("agency_id", "month_start", "route_id", name="uq_monthly_agency_month_route"),
        Index("ix_monthly_agency_route_month", "agency_id", "route_id", "month_start"),
        Index("ix_monthly_route_name", "route_name"),
    )


# ---------------------------------------------------------------------------
# Source Metadata and Audit Logs
# ---------------------------------------------------------------------------
class SourceMetadata(Base):
    __tablename__ = "source_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default", index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("agency_id", "key", name="uq_metadata_agency_key"),
    )


class DataAuditEvent(Base):
    __tablename__ = "data_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default", index=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    route_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_audit_agency_timestamp", "agency_id", "timestamp_utc"),
    )


class MonthlyImportEvent(Base):
    __tablename__ = "monthly_import_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[str] = mapped_column(String(64), nullable=False, default="cta", index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    source_info: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    incoming_records: Mapped[int] = mapped_column(Integer, nullable=False)
    added_records: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_records: Mapped[int] = mapped_column(Integer, nullable=False)
    unchanged_records: Mapped[int] = mapped_column(Integer, nullable=False)
    final_record_count: Mapped[int] = mapped_column(Integer, nullable=False)


class ModelArtifactRecord(Base):
    __tablename__ = "model_artifact_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default", index=True)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    trained_through: Mapped[date] = mapped_column(Date, nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    data_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ---------------------------------------------------------------------------
# GTFS Schedule Models (Phase 2 Transit Network)
# ---------------------------------------------------------------------------
class GTFSFeed(Base):
    __tablename__ = "gtfs_feeds"

    feed_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agency_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    feed_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    imported_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    agencies: Mapped[list["GTFSAgency"]] = relationship(back_populates="feed", cascade="all, delete-orphan")
    routes: Mapped[list["GTFSRoute"]] = relationship(back_populates="feed", cascade="all, delete-orphan")
    stops: Mapped[list["GTFSStop"]] = relationship(back_populates="feed", cascade="all, delete-orphan")
    trips: Mapped[list["GTFSTrip"]] = relationship(back_populates="feed", cascade="all, delete-orphan")
    stop_times: Mapped[list["GTFSStopTime"]] = relationship(back_populates="feed", cascade="all, delete-orphan")
    calendar: Mapped[list["GTFSCalendar"]] = relationship(back_populates="feed", cascade="all, delete-orphan")
    calendar_dates: Mapped[list["GTFSCalendarDate"]] = relationship(back_populates="feed", cascade="all, delete-orphan")
    shapes: Mapped[list["GTFSShape"]] = relationship(back_populates="feed", cascade="all, delete-orphan")


class GTFSAgency(Base):
    __tablename__ = "gtfs_agencies"

    feed_id: Mapped[str] = mapped_column(String(64), ForeignKey("gtfs_feeds.feed_id", ondelete="CASCADE"), primary_key=True)
    agency_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agency_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agency_url: Mapped[str] = mapped_column(String(512), nullable=False)
    agency_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    agency_lang: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    agency_phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    feed: Mapped["GTFSFeed"] = relationship(back_populates="agencies")


class GTFSRoute(Base):
    __tablename__ = "gtfs_routes"

    feed_id: Mapped[str] = mapped_column(String(64), ForeignKey("gtfs_feeds.feed_id", ondelete="CASCADE"), primary_key=True)
    route_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agency_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    route_short_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    route_long_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    route_type: Mapped[int] = mapped_column(Integer, nullable=False)
    route_color: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    route_text_color: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    feed: Mapped["GTFSFeed"] = relationship(back_populates="routes")


class GTFSStop(Base):
    __tablename__ = "gtfs_stops"

    feed_id: Mapped[str] = mapped_column(String(64), ForeignKey("gtfs_feeds.feed_id", ondelete="CASCADE"), primary_key=True)
    stop_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    stop_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stop_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    stop_lat: Mapped[float] = mapped_column(Float, nullable=False)
    stop_lon: Mapped[float] = mapped_column(Float, nullable=False)
    zone_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    location_type: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    feed: Mapped["GTFSFeed"] = relationship(back_populates="stops")

    __table_args__ = (
        Index("ix_gtfs_stop_coords", "feed_id", "stop_lat", "stop_lon"),
    )


class GTFSTrip(Base):
    __tablename__ = "gtfs_trips"

    feed_id: Mapped[str] = mapped_column(String(64), ForeignKey("gtfs_feeds.feed_id", ondelete="CASCADE"), primary_key=True)
    trip_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    route_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    service_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    trip_headsign: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    direction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shape_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    feed: Mapped["GTFSFeed"] = relationship(back_populates="trips")

    __table_args__ = (
        Index("ix_gtfs_trip_route_service", "feed_id", "route_id", "service_id"),
    )


class GTFSStopTime(Base):
    __tablename__ = "gtfs_stop_times"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_id: Mapped[str] = mapped_column(String(64), ForeignKey("gtfs_feeds.feed_id", ondelete="CASCADE"), index=True)
    trip_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stop_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stop_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    arrival_time: Mapped[str] = mapped_column(String(16), nullable=False)
    departure_time: Mapped[str] = mapped_column(String(16), nullable=False)
    pickup_type: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    drop_off_type: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    feed: Mapped["GTFSFeed"] = relationship(back_populates="stop_times")

    __table_args__ = (
        Index("ix_gtfs_stop_times_trip_seq", "feed_id", "trip_id", "stop_sequence"),
        Index("ix_gtfs_stop_times_stop", "feed_id", "stop_id"),
    )


class GTFSCalendar(Base):
    __tablename__ = "gtfs_calendar"

    feed_id: Mapped[str] = mapped_column(String(64), ForeignKey("gtfs_feeds.feed_id", ondelete="CASCADE"), primary_key=True)
    service_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    monday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tuesday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wednesday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    thursday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    friday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    saturday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sunday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    feed: Mapped["GTFSFeed"] = relationship(back_populates="calendar")


class GTFSCalendarDate(Base):
    __tablename__ = "gtfs_calendar_dates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_id: Mapped[str] = mapped_column(String(64), ForeignKey("gtfs_feeds.feed_id", ondelete="CASCADE"), index=True)
    service_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    exception_type: Mapped[int] = mapped_column(Integer, nullable=False)

    feed: Mapped["GTFSFeed"] = relationship(back_populates="calendar_dates")

    __table_args__ = (
        Index("ix_gtfs_calendar_dates_service_date", "feed_id", "service_id", "date"),
    )


class GTFSShape(Base):
    __tablename__ = "gtfs_shapes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_id: Mapped[str] = mapped_column(String(64), ForeignKey("gtfs_feeds.feed_id", ondelete="CASCADE"), index=True)
    shape_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    shape_pt_lat: Mapped[float] = mapped_column(Float, nullable=False)
    shape_pt_lon: Mapped[float] = mapped_column(Float, nullable=False)
    shape_pt_sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    feed: Mapped["GTFSFeed"] = relationship(back_populates="shapes")

    __table_args__ = (
        Index("ix_gtfs_shapes_shape_seq", "feed_id", "shape_id", "shape_pt_sequence"),
    )
