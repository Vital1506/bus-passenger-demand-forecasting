"""Standardized automated passenger counting (APC) and automated fare collection (AFC) observation pipeline.

Strict Engineering Guarantees:
- Strict schema & data type validation
- Duplicate record detection & idempotent upserting (never multiplies counts on re-ingestion)
- Trip-level & stop-level relational mapping
- Timestamp-based hourly aggregation ONLY when authentic timestamps exist
- Never derive or invent hourly distributions from daily totals alone
- Full audit event logging & job provenance tracking
"""

from __future__ import annotations

import hashlib
import io
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from .db.models import (
    DataAuditEvent,
    DailyPassengerRecord,
    ImportJobRecord,
    StopPassengerObservation,
    TripPassengerObservation,
)
from .db.repository import (
    complete_import_job,
    create_import_job,
    record_audit_event,
    upsert_daily_counts,
    upsert_stop_observations,
    upsert_trip_observations,
)

REQUIRED_APC_COLUMNS = {"route_id", "service_date", "boardings"}
OPTIONAL_APC_COLUMNS = {
    "trip_id",
    "stop_id",
    "stop_sequence",
    "vehicle_id",
    "alightings",
    "service_timestamp",
    "observation_timestamp",
}


@dataclass
class APCValidationResult:
    is_valid: bool
    total_records: int = 0
    valid_records: int = 0
    duplicate_records: int = 0
    rejected_records: int = 0
    routes_detected: list[str] = field(default_factory=list)
    date_range: tuple[str, str] | None = None
    has_timestamps: bool = False
    has_stop_level_data: bool = False
    has_trip_level_data: bool = False
    error_samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "total_records": self.total_records,
            "valid_records": self.valid_records,
            "duplicate_records": self.duplicate_records,
            "rejected_records": self.rejected_records,
            "routes_detected": self.routes_detected,
            "date_range": self.date_range,
            "has_timestamps": self.has_timestamps,
            "has_stop_level_data": self.has_stop_level_data,
            "has_trip_level_data": self.has_trip_level_data,
            "error_samples": self.error_samples[:15],
        }


def parse_and_validate_apc_data(
    raw_content: bytes | str | pd.DataFrame,
    agency_id: str = "default",
    source_system: str = "apc",
) -> tuple[pd.DataFrame, APCValidationResult]:
    """Parse CSV or DataFrame of passenger counts, validating schema and data types."""
    if isinstance(raw_content, pd.DataFrame):
        df = raw_content.copy()
    else:
        if isinstance(raw_content, bytes):
            text = raw_content.decode("utf-8-sig", errors="replace")
        else:
            text = raw_content
        try:
            df = pd.read_csv(io.StringIO(text))
        except Exception as exc:
            return pd.DataFrame(), APCValidationResult(
                is_valid=False,
                error_samples=[f"Malformed CSV syntax: {exc}"]
            )

    # Normalize column names (lowercase, stripped)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    # Map possible alternative column headers
    header_mapping = {
        "passengers": "boardings",
        "passenger_count": "boardings",
        "boarding_count": "boardings",
        "ons": "boardings",
        "alighting_count": "alightings",
        "offs": "alightings",
        "date": "service_date",
        "timestamp": "observation_timestamp",
        "datetime": "observation_timestamp",
        "service_time": "service_timestamp",
    }
    df = df.rename(columns={k: v for k, v in header_mapping.items() if k in df.columns and v not in df.columns})

    missing_required = REQUIRED_APC_COLUMNS - set(df.columns)
    if missing_required:
        return pd.DataFrame(), APCValidationResult(
            is_valid=False,
            error_samples=[f"Missing required APC columns: {sorted(missing_required)}"]
        )

    errors = []
    total = len(df)

    # 1. Clean route_id
    df["route_id"] = df["route_id"].astype(str).str.strip()
    empty_routes = df["route_id"].isin(["", "nan", "none", "null"])
    if empty_routes.any():
        errors.append(f"Found {int(empty_routes.sum())} records with blank route_id")

    # 2. Parse service_date (YYYY-MM-DD)
    try:
        df["service_date"] = pd.to_datetime(df["service_date"]).dt.date
    except Exception as exc:
        errors.append(f"Invalid service_date format: {exc}")

    # 3. Validate numeric boardings (non-negative, non-NaN, finite)
    boardings_numeric = pd.to_numeric(df["boardings"], errors="coerce")
    invalid_boardings = boardings_numeric.isna() | (boardings_numeric < 0) | np.isinf(boardings_numeric)
    if invalid_boardings.any():
        errors.append(f"Found {int(invalid_boardings.sum())} invalid or negative boardings values")
    df["boardings"] = boardings_numeric.fillna(0).astype(int)

    # 4. Optional alightings
    if "alightings" in df.columns:
        alightings_numeric = pd.to_numeric(df["alightings"], errors="coerce")
        invalid_alightings = alightings_numeric.isna() | (alightings_numeric < 0) | np.isinf(alightings_numeric)
        if invalid_alightings.any():
            errors.append(f"Found {int(invalid_alightings.sum())} invalid or negative alightings values")
        df["alightings"] = alightings_numeric.fillna(0).astype(int)
    else:
        df["alightings"] = 0

    # 5. Check timestamps
    has_timestamps = False
    ts_col = "observation_timestamp" if "observation_timestamp" in df.columns else ("service_timestamp" if "service_timestamp" in df.columns else None)
    if ts_col:
        try:
            df["observation_timestamp_utc"] = pd.to_datetime(df[ts_col], utc=True)
            has_timestamps = True
        except Exception as exc:
            errors.append(f"Could not parse timestamp column '{ts_col}': {exc}")
    else:
        df["observation_timestamp_utc"] = None

    # 6. Check trip_id & stop_id
    has_trip = "trip_id" in df.columns
    has_stop = "stop_id" in df.columns

    if has_trip:
        df["trip_id"] = df["trip_id"].astype(str).str.strip()
    else:
        df["trip_id"] = None

    if has_stop:
        df["stop_id"] = df["stop_id"].astype(str).str.strip()
        if "stop_sequence" in df.columns:
            df["stop_sequence"] = pd.to_numeric(df["stop_sequence"], errors="coerce").fillna(0).astype(int)
        else:
            df["stop_sequence"] = 0
    else:
        df["stop_id"] = None
        df["stop_sequence"] = 0

    if "vehicle_id" in df.columns:
        df["vehicle_id"] = df["vehicle_id"].astype(str).str.strip()
    else:
        df["vehicle_id"] = None

    # Filter out fatal errors
    valid_mask = ~empty_routes & ~invalid_boardings
    clean_df = df[valid_mask].copy()

    # Detect duplicates
    dup_cols = ["route_id", "service_date"]
    if has_trip:
        dup_cols.append("trip_id")
    if has_stop:
        dup_cols.append("stop_id")
    if has_timestamps:
        dup_cols.append("observation_timestamp_utc")

    dup_count = int(clean_df.duplicated(subset=dup_cols, keep="first").sum())
    clean_df = clean_df.drop_duplicates(subset=dup_cols, keep="last")

    date_min = str(clean_df["service_date"].min()) if not clean_df.empty else None
    date_max = str(clean_df["service_date"].max()) if not clean_df.empty else None
    routes = sorted([str(r) for r in clean_df["route_id"].unique()])

    val_res = APCValidationResult(
        is_valid=len(errors) == 0 or len(clean_df) > 0,
        total_records=total,
        valid_records=len(clean_df),
        duplicate_records=dup_count,
        rejected_records=total - len(clean_df),
        routes_detected=routes,
        date_range=(date_min, date_max) if date_min and date_max else None,
        has_timestamps=has_timestamps,
        has_stop_level_data=has_stop,
        has_trip_level_data=has_trip,
        error_samples=errors,
    )
    return clean_df, val_res


def ingest_apc_dataset(
    session: Session,
    df: pd.DataFrame,
    agency_id: str = "default",
    source_system: str = "apc",
    source_filename: str = "apc_upload.csv",
) -> dict[str, Any]:
    """Ingest validated APC dataframe into relational database idempotently."""
    if df.empty:
        return {"status": "SKIPPED", "records_ingested": 0, "message": "No valid records to ingest"}

    job_id = f"JOB-APC-{uuid.uuid4().hex[:12].upper()}"
    content_bytes = df.to_csv(index=False).encode("utf-8")
    sha256_hash = hashlib.sha256(content_bytes).hexdigest()

    create_import_job(
        session=session,
        job_id=job_id,
        job_type="apc_batch",
        source_filename=source_filename,
        sha256_hash=sha256_hash,
        agency_id=agency_id,
    )

    inserted_trips = 0
    updated_trips = 0
    inserted_stops = 0

    try:
        # 1. Stop-level observations if present
        has_stops = "stop_id" in df.columns and df["stop_id"].notna().any()
        if has_stops:
            stop_rows = []
            for _, r in df[df["stop_id"].notna()].iterrows():
                stop_rows.append({
                    "route_id": str(r["route_id"]),
                    "trip_id": str(r.get("trip_id") or "UNKNOWN"),
                    "stop_id": str(r["stop_id"]),
                    "stop_sequence": int(r.get("stop_sequence", 0)),
                    "observation_timestamp_utc": r.get("observation_timestamp_utc"),
                    "boardings": int(r["boardings"]),
                    "alightings": int(r.get("alightings", 0)),
                    "vehicle_id": r.get("vehicle_id"),
                })
            inserted_stops = upsert_stop_observations(
                session=session,
                observations=stop_rows,
                agency_id=agency_id,
                source_system=source_system,
            )

        # 2. Trip-level observations if present
        has_trips = "trip_id" in df.columns and df["trip_id"].notna().any()
        if has_trips:
            # Group by trip and service date
            trip_groups = df[df["trip_id"].notna()].groupby(["route_id", "trip_id", "service_date"])
            trip_rows = []
            for (r_id, t_id, s_date), group in trip_groups:
                trip_rows.append({
                    "route_id": str(r_id),
                    "trip_id": str(t_id),
                    "service_date": s_date,
                    "total_boardings": int(group["boardings"].sum()),
                    "total_alightings": int(group["alightings"].sum()),
                    "vehicle_id": group["vehicle_id"].dropna().iloc[0] if "vehicle_id" in group.columns and not group["vehicle_id"].dropna().empty else None,
                })
            inserted_trips, updated_trips = upsert_trip_observations(
                session=session,
                observations=trip_rows,
                agency_id=agency_id,
                source_system=source_system,
            )

        # 3. Aggregate route-day totals for daily passenger records (idempotent upsert)
        daily_groups = df.groupby(["route_id", "service_date"])["boardings"].sum().reset_index()
        daily_records = [
            {
                "route_id": str(row["route_id"]),
                "service_date": row["service_date"],
                "passengers": float(row["boardings"]),
            }
            for _, row in daily_groups.iterrows()
        ]
        inserted_daily, updated_daily = upsert_daily_counts(
            session=session,
            records=daily_records,
            agency_id=agency_id,
            source_system=source_system,
        )

        # Audit event logging
        record_audit_event(
            session=session,
            action="apc_ingest",
            source_system=source_system,
            record_count=len(df),
            route_count=int(df["route_id"].nunique()),
            agency_id=agency_id,
            details={"job_id": job_id, "sha256": sha256_hash, "trips_upserted": inserted_trips + updated_trips, "stops_inserted": inserted_stops},
        )

        complete_import_job(
            session=session,
            job_id=job_id,
            status="COMPLETED",
            records_processed=len(df),
            records_inserted=inserted_daily + inserted_trips + inserted_stops,
            records_updated=updated_daily + updated_trips,
        )

        session.commit()
        return {
            "status": "COMPLETED",
            "job_id": job_id,
            "records_processed": len(df),
            "daily_records": {"inserted": inserted_daily, "updated": updated_daily},
            "trip_records": {"inserted": inserted_trips, "updated": updated_trips},
            "stop_records": {"inserted": inserted_stops},
        }

    except Exception as exc:
        session.rollback()
        complete_import_job(
            session=session,
            job_id=job_id,
            status="FAILED",
            error_message=str(exc),
        )
        session.commit()
        raise exc


@dataclass
class ETMValidationResult:
    is_valid: bool
    total_transactions: int = 0
    valid_transactions: int = 0
    rejected_transactions: int = 0
    aggregated_hourly_buckets: int = 0
    total_boardings: int = 0
    routes_detected: list[str] = field(default_factory=list)
    date_range: tuple[str, str] | None = None
    error_samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "total_transactions": self.total_transactions,
            "valid_transactions": self.valid_transactions,
            "rejected_transactions": self.rejected_transactions,
            "aggregated_hourly_buckets": self.aggregated_hourly_buckets,
            "total_boardings": self.total_boardings,
            "routes_detected": self.routes_detected,
            "date_range": self.date_range,
            "error_samples": self.error_samples[:15],
        }


def parse_and_aggregate_etm_data(
    raw_content: bytes | str | pd.DataFrame,
    agency_id: str = "default",
) -> tuple[pd.DataFrame, ETMValidationResult]:
    """Parse raw Electronic Ticketing Machine (ETM) transaction logs and aggregate into hourly boardings."""
    if isinstance(raw_content, pd.DataFrame):
        df = raw_content.copy()
    else:
        if isinstance(raw_content, bytes):
            text = raw_content.decode("utf-8-sig", errors="replace")
        else:
            text = str(raw_content)
        try:
            df = pd.read_csv(io.StringIO(text))
        except Exception as exc:
            return pd.DataFrame(), ETMValidationResult(
                is_valid=False,
                error_samples=[f"Malformed CSV syntax: {exc}"]
            )

    if df.empty:
        return pd.DataFrame(), ETMValidationResult(
            is_valid=False,
            error_samples=["Uploaded ETM file is empty"]
        )

    # Normalize column names
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    # Map vendor-specific column synonyms
    header_mapping = {
        "route": "route_id",
        "line": "route_id",
        "route_no": "route_id",
        "timestamp": "transaction_time",
        "datetime": "transaction_time",
        "issue_time": "transaction_time",
        "tap_time": "transaction_time",
        "ticket_time": "transaction_time",
        "time": "transaction_time",
        "ticket_id": "ticket_id",
        "transaction_id": "ticket_id",
        "ticket_no": "ticket_id",
        "quantity": "boardings",
        "passengers": "boardings",
        "passenger_count": "boardings",
        "count": "boardings",
        "stop": "stop_id",
        "stop_code": "stop_id",
        "stage": "stop_id",
        "station_id": "stop_id",
        "vehicle": "vehicle_id",
        "bus_id": "vehicle_id",
        "machine_id": "vehicle_id",
    }
    df = df.rename(columns={k: v for k, v in header_mapping.items() if k in df.columns and v not in df.columns})

    errors = []
    total = len(df)

    if "route_id" not in df.columns:
        errors.append("Missing required route identifier column ('route_id' or 'route')")
        return pd.DataFrame(), ETMValidationResult(
            is_valid=False,
            total_transactions=total,
            rejected_transactions=total,
            error_samples=errors,
        )

    if "transaction_time" not in df.columns:
        errors.append("Missing required transaction timestamp column ('timestamp', 'transaction_time', 'datetime')")
        return pd.DataFrame(), ETMValidationResult(
            is_valid=False,
            total_transactions=total,
            rejected_transactions=total,
            error_samples=errors,
        )

    # 1. Clean route_id
    df["route_id"] = df["route_id"].astype(str).str.strip()
    valid_route_mask = ~df["route_id"].isin(["", "nan", "none", "null"])

    # 2. Parse timestamps
    df["parsed_time"] = pd.to_datetime(df["transaction_time"], errors="coerce")
    valid_time_mask = df["parsed_time"].notna()

    # 3. Clean quantities / boardings
    if "boardings" in df.columns:
        boardings_numeric = pd.to_numeric(df["boardings"], errors="coerce").fillna(1).astype(int)
        df["boardings"] = np.maximum(1, boardings_numeric)
    else:
        df["boardings"] = 1

    valid_mask = valid_route_mask & valid_time_mask
    valid_df = df[valid_mask].copy()
    rejected_count = total - len(valid_df)

    if rejected_count > 0:
        errors.append(f"Rejected {rejected_count} transactions due to invalid route_id or unparseable timestamp")

    if valid_df.empty:
        return pd.DataFrame(), ETMValidationResult(
            is_valid=False,
            total_transactions=total,
            valid_transactions=0,
            rejected_transactions=total,
            error_samples=errors,
        )

    # Extract service_date and hour
    valid_df["service_date"] = valid_df["parsed_time"].dt.date
    valid_df["hour"] = valid_df["parsed_time"].dt.hour
    valid_df["agency_id"] = agency_id

    # Group by (agency_id, route_id, service_date, hour)
    ticket_col = "ticket_id" if "ticket_id" in valid_df.columns else "boardings"
    grouped = valid_df.groupby(["agency_id", "route_id", "service_date", "hour"]).agg(
        boardings=("boardings", "sum"),
        transaction_count=(ticket_col, "count"),
    ).reset_index()

    # Sort
    grouped = grouped.sort_values(by=["route_id", "service_date", "hour"]).reset_index(drop=True)

    date_min = str(grouped["service_date"].min())
    date_max = str(grouped["service_date"].max())
    routes = sorted([str(r) for r in grouped["route_id"].unique()])
    total_boardings = int(grouped["boardings"].sum())

    res = ETMValidationResult(
        is_valid=True,
        total_transactions=total,
        valid_transactions=len(valid_df),
        rejected_transactions=rejected_count,
        aggregated_hourly_buckets=len(grouped),
        total_boardings=total_boardings,
        routes_detected=routes,
        date_range=(date_min, date_max),
        error_samples=errors,
    )
    return grouped, res


def aggregate_etm_to_daily(etm_hourly_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate ETM hourly boardings dataframe into daily passenger totals for forecasting models."""
    if etm_hourly_df.empty:
        return pd.DataFrame(columns=["agency_id", "route_id", "service_date", "boardings"])
    daily = etm_hourly_df.groupby(["agency_id", "route_id", "service_date"]).agg(
        boardings=("boardings", "sum")
    ).reset_index()
    daily = daily.sort_values(by=["route_id", "service_date"]).reset_index(drop=True)
    return daily


def ingest_etm_dataset(
    session: Session,
    hourly_df: pd.DataFrame,
    agency_id: str = "default",
    source_filename: str = "etm_upload.csv",
) -> dict[str, Any]:
    """Ingest aggregated ETM dataset into relational daily records."""
    if hourly_df.empty:
        return {"status": "SKIPPED", "records_ingested": 0, "message": "No valid ETM records to ingest"}

    daily_df = aggregate_etm_to_daily(hourly_df)
    job_id = f"JOB-ETM-{uuid.uuid4().hex[:12].upper()}"
    content_bytes = daily_df.to_csv(index=False).encode("utf-8")
    sha256_hash = hashlib.sha256(content_bytes).hexdigest()

    create_import_job(
        session=session,
        job_id=job_id,
        job_type="etm_batch",
        source_filename=source_filename,
        sha256_hash=sha256_hash,
        agency_id=agency_id,
    )

    try:
        inserted_daily, updated_daily = upsert_daily_counts(
            session=session,
            df=daily_df,
            agency_id=agency_id,
            source_system="etm",
        )
        record_audit_event(
            session=session,
            action="etm_ingest",
            source_system="etm",
            record_count=len(daily_df),
            route_count=int(daily_df["route_id"].nunique()),
            agency_id=agency_id,
            details={"job_id": job_id, "sha256": sha256_hash, "hourly_buckets": len(hourly_df)},
        )
        complete_import_job(
            session=session,
            job_id=job_id,
            status="COMPLETED",
            records_processed=len(daily_df),
            records_inserted=inserted_daily,
            records_updated=updated_daily,
        )
        session.commit()
        return {
            "status": "COMPLETED",
            "job_id": job_id,
            "hourly_buckets": len(hourly_df),
            "daily_records_upserted": inserted_daily + updated_daily,
            "routes_detected": sorted([str(r) for r in daily_df["route_id"].unique()]),
        }
    except Exception as exc:
        session.rollback()
        complete_import_job(
            session=session,
            job_id=job_id,
            status="FAILED",
            error_message=str(exc),
        )
        session.commit()
        raise exc
