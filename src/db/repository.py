"""High-level, transactional repository operations for daily and monthly passenger data."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import (
    AgencyRecord,
    DailyPassengerRecord,
    DataAuditEvent,
    DataSourceRecord,
    ForecastRecord,
    ImportJobRecord,
    ModelArtifactRecord,
    MonthlyImportEvent,
    MonthlyPassengerRecord,
    OperationalAlertRecord,
    SourceMetadata,
    StopPassengerObservation,
    TripPassengerObservation,
    UserAccountRecord,
    VehicleRecord,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# Daily Passenger Operations
# ===========================================================================

def upsert_daily_counts(
    session: Session,
    records: list[dict[str, Any]],
    agency_id: str = "default",
    source_system: str = "manual_csv",
) -> tuple[int, int]:
    """Idempotently insert or update daily passenger counts within the active transaction.

    Returns: (inserted_count, updated_count)
    """
    if not records:
        return 0, 0

    now = _utc_now()
    inserted = 0
    updated = 0

    # Read existing keys in one query for batch reconciliation
    dates = {
        r["service_date"] if isinstance(r["service_date"], date) else pd.to_datetime(r["service_date"]).date()
        for r in records
    }
    routes = {str(r["route_id"]).strip() for r in records}

    existing_rows = session.execute(
        select(DailyPassengerRecord).where(
            DailyPassengerRecord.agency_id == agency_id,
            DailyPassengerRecord.service_date.in_(dates),
            DailyPassengerRecord.route_id.in_(routes),
        )
    ).scalars().all()

    existing_map = {(row.service_date, row.route_id): row for row in existing_rows}

    for record in records:
        s_date = record["service_date"] if isinstance(record["service_date"], date) else pd.to_datetime(record["service_date"]).date()
        r_id = str(record["route_id"]).strip()
        passengers = float(record["passengers"])

        key = (s_date, r_id)
        if key in existing_map:
            row = existing_map[key]
            row.passengers = passengers
            row.source_system = source_system
            row.updated_at_utc = now
            updated += 1
        else:
            new_row = DailyPassengerRecord(
                agency_id=agency_id,
                service_date=s_date,
                route_id=r_id,
                passengers=passengers,
                source_system=source_system,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(new_row)
            existing_map[key] = new_row
            inserted += 1

    session.flush()
    return inserted, updated


def replace_all_daily_counts(
    session: Session,
    records: list[dict[str, Any]],
    agency_id: str = "default",
    source_system: str = "manual_csv",
) -> int:
    """Atomic replacement of all daily observations for an agency."""
    session.execute(
        delete(DailyPassengerRecord).where(DailyPassengerRecord.agency_id == agency_id)
    )
    now = _utc_now()
    items = []
    for r in records:
        s_date = r["service_date"] if isinstance(r["service_date"], date) else pd.to_datetime(r["service_date"]).date()
        items.append(
            DailyPassengerRecord(
                agency_id=agency_id,
                service_date=s_date,
                route_id=str(r["route_id"]).strip(),
                passengers=float(r["passengers"]),
                source_system=source_system,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
    session.add_all(items)
    session.flush()
    return len(items)


def get_daily_counts_df(session: Session, agency_id: str = "default") -> pd.DataFrame:
    """Load daily records into a pandas DataFrame matching the forecasting contract."""
    rows = session.execute(
        select(DailyPassengerRecord.service_date, DailyPassengerRecord.route_id, DailyPassengerRecord.passengers)
        .where(DailyPassengerRecord.agency_id == agency_id)
        .order_by(DailyPassengerRecord.route_id, DailyPassengerRecord.service_date)
    ).all()

    if not rows:
        return pd.DataFrame(columns=["service_date", "route_id", "passengers"])

    df = pd.DataFrame(rows, columns=["service_date", "route_id", "passengers"])
    df["service_date"] = pd.to_datetime(df["service_date"])
    df["route_id"] = df["route_id"].astype(str)
    df["passengers"] = pd.to_numeric(df["passengers"], errors="coerce")
    return df


def get_daily_counts_summary(session: Session, agency_id: str = "default") -> dict:
    """Quick summary metrics for status reporting."""
    res = session.execute(
        select(
            func.count(DailyPassengerRecord.id),
            func.count(func.distinct(DailyPassengerRecord.route_id)),
            func.min(DailyPassengerRecord.service_date),
            func.max(DailyPassengerRecord.service_date),
        ).where(DailyPassengerRecord.agency_id == agency_id)
    ).first()

    count, route_count, min_date, max_date = res if res else (0, 0, None, None)
    return {
        "rows": count or 0,
        "route_count": route_count or 0,
        "earliest_date": min_date.isoformat() if min_date else None,
        "latest_date": max_date.isoformat() if max_date else None,
    }


# ===========================================================================
# Monthly Passenger Operations
# ===========================================================================

def upsert_monthly_records(
    session: Session,
    records: list[dict[str, Any]],
    agency_id: str = "cta",
) -> tuple[int, int, int]:
    """Upsert monthly observations; returns (added, updated, unchanged)."""
    if not records:
        return 0, 0, 0

    now = _utc_now()
    added = updated = unchanged = 0

    # Map existing records for the agency
    existing_rows = session.execute(
        select(MonthlyPassengerRecord).where(MonthlyPassengerRecord.agency_id == agency_id)
    ).scalars().all()
    existing_map = {(row.month_start, row.route_id): row for row in existing_rows}

    for item in records:
        m_start = item["month_start"] if isinstance(item["month_start"], date) else pd.to_datetime(item["month_start"]).date()
        r_id = str(item["route_id"]).strip()
        r_name = str(item.get("route_name", "")).strip()
        boardings_str = str(item["boardings"]).strip()
        try:
            boardings_num = float(boardings_str.replace(",", ""))
        except (ValueError, TypeError):
            boardings_num = 0.0

        key = (m_start, r_id)
        if key in existing_map:
            row = existing_map[key]
            if row.route_name == r_name and row.boardings == boardings_str:
                unchanged += 1
            else:
                row.route_name = r_name
                row.boardings = boardings_str
                row.boardings_numeric = boardings_num
                row.updated_at_utc = now
                updated += 1
        else:
            new_row = MonthlyPassengerRecord(
                agency_id=agency_id,
                month_start=m_start,
                route_id=r_id,
                route_name=r_name,
                boardings=boardings_str,
                boardings_numeric=boardings_num,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(new_row)
            existing_map[key] = new_row
            added += 1

    session.flush()
    return added, updated, unchanged


def get_monthly_observations(session: Session, agency_id: str = "cta") -> list[dict]:
    """Fetch all monthly observations in a clean dict format."""
    rows = session.execute(
        select(MonthlyPassengerRecord)
        .where(MonthlyPassengerRecord.agency_id == agency_id)
        .order_by(MonthlyPassengerRecord.month_start, MonthlyPassengerRecord.route_id)
    ).scalars().all()

    return [
        {
            "month_start": row.month_start.isoformat(),
            "route_id": row.route_id,
            "route_name": row.route_name,
            "boardings": row.boardings,
        }
        for row in rows
    ]


def get_monthly_routes_summary(
    session: Session,
    q: str | None = None,
    limit: int = 25,
    agency_id: str = "cta",
) -> dict:
    """Search and summarize routes from monthly dataset."""
    records = get_monthly_observations(session, agency_id=agency_id)

    by_route: dict[str, list[dict]] = {}
    for record in records:
        by_route.setdefault(record["route_id"], []).append(record)

    def _route_names(items: list[dict]) -> list[str]:
        names = []
        seen = set()
        for item in items:
            name = str(item.get("route_name", "")).strip()
            norm = name.casefold()
            if name and norm not in seen:
                names.append(name)
                seen.add(norm)
        return names

    query = (q or "").strip().casefold()
    matching = []
    for route_id, route_records in by_route.items():
        ordered = sorted(route_records, key=lambda r: r["month_start"])
        first_month = ordered[0]["month_start"]
        latest = ordered[-1]
        hist_names = _route_names(ordered)
        current_name = next((r["route_name"].strip() for r in reversed(ordered) if r["route_name"].strip()), "")
        searchable = [route_id, *hist_names]
        if not query or any(query in val.casefold() for val in searchable):
            matching.append({
                "route_id": route_id,
                "route_name": current_name,
                "historical_route_names": hist_names,
                "first_month": first_month,
                "last_month": latest["month_start"],
                "observed_months": len(ordered),
                "latest_month": latest["month_start"],
                "latest_boardings": latest["boardings"],
            })

    matching.sort(key=lambda r: (r["route_id"].casefold(), r["route_id"]))
    return {
        "query": q,
        "count": min(len(matching), limit),
        "total_matches": len(matching),
        "limit": limit,
        "routes": matching[:limit],
    }


def get_monthly_route_history(
    session: Session,
    route_id: str,
    agency_id: str = "cta",
) -> dict | None:
    """Chronological monthly history for one route."""
    norm_id = route_id.strip()
    rows = session.execute(
        select(MonthlyPassengerRecord)
        .where(
            MonthlyPassengerRecord.agency_id == agency_id,
            MonthlyPassengerRecord.route_id == norm_id,
        )
        .order_by(MonthlyPassengerRecord.month_start)
    ).scalars().all()

    if not rows:
        return None

    def _route_names(items: list[MonthlyPassengerRecord]) -> list[str]:
        names = []
        seen = set()
        for item in items:
            name = item.route_name.strip()
            norm = name.casefold()
            if name and norm not in seen:
                names.append(name)
                seen.add(norm)
        return names

    ordered = [
        {
            "month_start": r.month_start.isoformat(),
            "route_name": r.route_name,
            "boardings": r.boardings,
        }
        for r in rows
    ]

    first_m = ordered[0]["month_start"]
    last_m = ordered[-1]["month_start"]

    def _m_idx(m_str: str) -> int:
        y, m, _ = (int(p) for p in m_str.split("-"))
        return y * 12 + m - 1

    span = _m_idx(last_m) - _m_idx(first_m) + 1
    observed = len(ordered)

    return {
        "route_id": norm_id,
        "route_name": next((r["route_name"].strip() for r in reversed(ordered) if r["route_name"].strip()), ""),
        "historical_route_names": _route_names(rows),
        "first_month": first_m,
        "last_month": last_m,
        "observed_months": observed,
        "missing_months": span - observed,
        "boardings_description": "Recorded monthly boardings; these are boardings, not unique passengers.",
        "records": ordered,
    }


def get_monthly_store_status(session: Session, agency_id: str = "cta") -> dict:
    """Store status and metadata."""
    res = session.execute(
        select(
            func.count(MonthlyPassengerRecord.id),
            func.count(func.distinct(MonthlyPassengerRecord.route_id)),
            func.min(MonthlyPassengerRecord.month_start),
            func.max(MonthlyPassengerRecord.month_start),
        ).where(MonthlyPassengerRecord.agency_id == agency_id)
    ).first()

    rec_count, route_count, min_m, max_m = res if res else (0, 0, None, None)

    latest_import = session.execute(
        select(MonthlyImportEvent)
        .where(MonthlyImportEvent.agency_id == agency_id)
        .order_by(MonthlyImportEvent.uploaded_at_utc.desc())
        .limit(1)
    ).scalar_one_or_none()

    import_info = None
    if latest_import:
        import_info = {
            "original_filename": latest_import.original_filename,
            "uploaded_at_utc": latest_import.uploaded_at_utc.isoformat(),
            "source_info": latest_import.source_info,
            "sha256": latest_import.sha256,
            "incoming_records": latest_import.incoming_records,
            "added_records": latest_import.added_records,
            "updated_records": latest_import.updated_records,
            "unchanged_records": latest_import.unchanged_records,
            "final_record_count": latest_import.final_record_count,
        }

    return {
        "dataset": "Chicago CTA monthly bus ridership",
        "granularity": "monthly",
        "storage_exists": (rec_count or 0) > 0,
        "record_count": rec_count or 0,
        "route_count": route_count or 0,
        "first_month": min_m.isoformat() if min_m else None,
        "latest_month": max_m.isoformat() if max_m else None,
        "last_import": import_info,
    }


# ===========================================================================
# Audit and Metadata Operations
# ===========================================================================

def record_audit_event(
    session: Session,
    action: str,
    source_system: str,
    record_count: int,
    route_count: int,
    agency_id: str = "default",
    details: dict | None = None,
) -> None:
    """Record an audit event into database."""
    session.add(
        DataAuditEvent(
            agency_id=agency_id,
            timestamp_utc=_utc_now(),
            action=action,
            source_system=source_system,
            record_count=int(record_count),
            route_count=int(route_count),
            details_json=json.dumps(details) if details else None,
        )
    )
    session.flush()


def get_recent_audit_events(
    session: Session,
    limit: int = 12,
    agency_id: str = "default",
) -> list[dict]:
    """Retrieve recent aggregate audit events."""
    rows = session.execute(
        select(DataAuditEvent)
        .where(DataAuditEvent.agency_id == agency_id)
        .order_by(DataAuditEvent.timestamp_utc.desc())
        .limit(limit)
    ).scalars().all()

    return [
        {
            "timestamp_utc": r.timestamp_utc.isoformat(),
            "action": r.action,
            "source_system": r.source_system,
            "record_count": r.record_count,
            "route_count": r.route_count,
        }
        for r in rows
    ]


def write_source_metadata(
    session: Session,
    metadata: dict,
    agency_id: str = "default",
) -> None:
    """Store or update source metadata."""
    now = _utc_now()
    existing = session.execute(
        select(SourceMetadata).where(
            SourceMetadata.agency_id == agency_id,
            SourceMetadata.key == "data_source",
        )
    ).scalar_one_or_none()

    if existing:
        existing.value_json = json.dumps(metadata)
        existing.updated_at_utc = now
    else:
        session.add(
            SourceMetadata(
                agency_id=agency_id,
                key="data_source",
                value_json=json.dumps(metadata),
                updated_at_utc=now,
            )
        )
    session.flush()


def read_source_metadata(
    session: Session,
    agency_id: str = "default",
) -> dict:
    """Read source metadata or return empty default."""
    row = session.execute(
        select(SourceMetadata).where(
            SourceMetadata.agency_id == agency_id,
            SourceMetadata.key == "data_source",
        )
    ).scalar_one_or_none()

    if not row:
        return {"source": "none"}
    try:
        return json.loads(row.value_json)
    except Exception:
        return {"source": "unknown"}


def record_monthly_import_event(
    session: Session,
    event_data: dict,
    agency_id: str = "cta",
) -> None:
    """Save record of monthly import event."""
    session.add(
        MonthlyImportEvent(
            agency_id=agency_id,
            original_filename=event_data["original_filename"],
            uploaded_at_utc=datetime.fromisoformat(event_data["uploaded_at_utc"].replace("Z", "+00:00")),
            source_info=event_data.get("source_info"),
            sha256=event_data["sha256"],
            incoming_records=event_data["incoming_records"],
            added_records=event_data["added_records"],
            updated_records=event_data["updated_records"],
            unchanged_records=event_data["unchanged_records"],
            final_record_count=event_data["final_record_count"],
        )
    )
    session.flush()


# ===========================================================================
# Enterprise Multi-Agency, Fleet & Observation Repository Functions
# ===========================================================================

def upsert_agency(
    session: Session,
    agency_id: str,
    agency_name: str,
    timezone_str: str = "UTC",
    contact_email: str | None = None,
) -> AgencyRecord:
    """Create or update a tenant transit agency."""
    existing = session.execute(
        select(AgencyRecord).where(AgencyRecord.agency_id == agency_id)
    ).scalar_one_or_none()

    now = _utc_now()
    if existing:
        existing.agency_name = agency_name
        existing.timezone = timezone_str
        existing.contact_email = contact_email
        existing.updated_at_utc = now
        session.flush()
        return existing

    agency = AgencyRecord(
        agency_id=agency_id,
        agency_name=agency_name,
        timezone=timezone_str,
        contact_email=contact_email,
        is_active=True,
        created_at_utc=now,
        updated_at_utc=now,
    )
    session.add(agency)
    session.flush()
    return agency


def get_agencies(session: Session, active_only: bool = True) -> list[dict[str, Any]]:
    """List registered transit agencies."""
    stmt = select(AgencyRecord)
    if active_only:
        stmt = stmt.where(AgencyRecord.is_active == True)  # noqa: E712
    records = session.execute(stmt.order_by(AgencyRecord.agency_name)).scalars().all()
    return [
        {
            "agency_id": a.agency_id,
            "agency_name": a.agency_name,
            "timezone": a.timezone,
            "contact_email": a.contact_email,
            "is_active": a.is_active,
            "created_at_utc": a.created_at_utc.isoformat(),
        }
        for a in records
    ]


def upsert_vehicle(
    session: Session,
    agency_id: str,
    vehicle_id: str,
    label: str | None = None,
    model: str | None = None,
    seated_capacity: int = 40,
    standing_capacity: int = 15,
    total_capacity: int = 55,
) -> VehicleRecord:
    """Register or update a vehicle with verified capacities."""
    existing = session.execute(
        select(VehicleRecord).where(
            VehicleRecord.agency_id == agency_id,
            VehicleRecord.vehicle_id == vehicle_id,
        )
    ).scalar_one_or_none()

    if existing:
        existing.label = label or existing.label
        existing.model = model or existing.model
        existing.seated_capacity = seated_capacity
        existing.standing_capacity = standing_capacity
        existing.total_capacity = total_capacity
        session.flush()
        return existing

    vehicle = VehicleRecord(
        agency_id=agency_id,
        vehicle_id=vehicle_id,
        label=label,
        model=model,
        seated_capacity=seated_capacity,
        standing_capacity=standing_capacity,
        total_capacity=total_capacity,
        is_active=True,
        created_at_utc=_utc_now(),
    )
    session.add(vehicle)
    session.flush()
    return vehicle


def get_vehicle_capacity(session: Session, agency_id: str, vehicle_id: str) -> dict[str, int]:
    """Retrieve verified vehicle capacity configuration."""
    row = session.execute(
        select(VehicleRecord).where(
            VehicleRecord.agency_id == agency_id,
            VehicleRecord.vehicle_id == vehicle_id,
        )
    ).scalar_one_or_none()

    if row:
        return {
            "seated_capacity": row.seated_capacity,
            "standing_capacity": row.standing_capacity,
            "total_capacity": row.total_capacity,
        }
    return {
        "seated_capacity": 40,
        "standing_capacity": 15,
        "total_capacity": 55,
    }


def upsert_trip_observations(
    session: Session,
    observations: list[dict[str, Any]],
    agency_id: str = "default",
    source_system: str = "apc",
) -> tuple[int, int]:
    """Idempotently insert trip-level passenger observations."""
    if not observations:
        return 0, 0

    now = _utc_now()
    inserted = 0
    updated = 0

    for obs in observations:
        t_id = str(obs["trip_id"]).strip()
        s_date = obs["service_date"] if isinstance(obs["service_date"], date) else pd.to_datetime(obs["service_date"]).date()

        existing = session.execute(
            select(TripPassengerObservation).where(
                TripPassengerObservation.agency_id == agency_id,
                TripPassengerObservation.trip_id == t_id,
                TripPassengerObservation.service_date == s_date,
                TripPassengerObservation.source_system == source_system,
            )
        ).scalar_one_or_none()

        if existing:
            existing.total_boardings = int(obs.get("total_boardings", 0))
            existing.total_alightings = int(obs.get("total_alightings", 0))
            existing.vehicle_id = obs.get("vehicle_id")
            updated += 1
        else:
            new_obs = TripPassengerObservation(
                agency_id=agency_id,
                trip_id=t_id,
                route_id=str(obs["route_id"]).strip(),
                vehicle_id=obs.get("vehicle_id"),
                service_date=s_date,
                total_boardings=int(obs.get("total_boardings", 0)),
                total_alightings=int(obs.get("total_alightings", 0)),
                source_system=source_system,
                created_at_utc=now,
            )
            session.add(new_obs)
            inserted += 1

    session.flush()
    return inserted, updated


def upsert_stop_observations(
    session: Session,
    observations: list[dict[str, Any]],
    agency_id: str = "default",
    source_system: str = "apc",
) -> int:
    """Insert stop-level boarding and alighting observations."""
    if not observations:
        return 0

    now = _utc_now()
    count = 0
    for obs in observations:
        ts = obs.get("observation_timestamp_utc")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        elif not isinstance(ts, datetime):
            ts = now

        row = StopPassengerObservation(
            agency_id=agency_id,
            route_id=str(obs["route_id"]).strip(),
            trip_id=str(obs["trip_id"]).strip(),
            stop_id=str(obs["stop_id"]).strip(),
            stop_sequence=int(obs.get("stop_sequence", 0)),
            observation_timestamp_utc=ts,
            boardings=int(obs.get("boardings", 0)),
            alightings=int(obs.get("alightings", 0)),
            onboard_occupancy=obs.get("onboard_occupancy"),
            vehicle_id=obs.get("vehicle_id"),
            source_system=source_system,
            created_at_utc=now,
        )
        session.add(row)
        count += 1

    session.flush()
    return count


def record_forecast_run(
    session: Session,
    forecasts: list[dict[str, Any]],
    agency_id: str = "default",
) -> int:
    """Persist generated demand forecasts."""
    if not forecasts:
        return 0

    now = _utc_now()
    count = 0
    for fc in forecasts:
        f_date = fc["forecast_date"] if isinstance(fc["forecast_date"], date) else pd.to_datetime(fc["forecast_date"]).date()
        row = ForecastRecord(
            agency_id=agency_id,
            route_id=str(fc["route_id"]).strip(),
            forecast_date=f_date,
            forecast_horizon_days=int(fc.get("forecast_horizon_days", 1)),
            predicted_demand=float(fc["predicted_demand"]),
            lower_bound=float(fc["lower_bound"]) if fc.get("lower_bound") is not None else None,
            upper_bound=float(fc["upper_bound"]) if fc.get("upper_bound") is not None else None,
            model_version=str(fc.get("model_version", "v1")),
            created_at_utc=now,
        )
        session.add(row)
        count += 1

    session.flush()
    return count


def record_operational_alert_in_db(
    session: Session,
    alert_dict: dict[str, Any],
    agency_id: str = "default",
) -> OperationalAlertRecord:
    """Save an operational anomaly or dispatch alert to database."""
    now = _utc_now()
    alert_id = alert_dict.get("alert_id") or f"ALT-{int(now.timestamp()*1000)}"

    existing = session.execute(
        select(OperationalAlertRecord).where(
            OperationalAlertRecord.agency_id == agency_id,
            OperationalAlertRecord.alert_id == alert_id,
        )
    ).scalar_one_or_none()

    if existing:
        return existing

    record = OperationalAlertRecord(
        agency_id=agency_id,
        alert_id=alert_id,
        alert_type=alert_dict.get("anomaly_type") or alert_dict.get("alert_type", "GENERAL"),
        severity=alert_dict.get("severity", "MEDIUM"),
        route_id=alert_dict.get("route_id"),
        trip_id=alert_dict.get("trip_id"),
        vehicle_id=",".join(alert_dict.get("vehicle_ids", [])) if isinstance(alert_dict.get("vehicle_ids"), list) else alert_dict.get("vehicle_id"),
        summary=alert_dict.get("description") or alert_dict.get("summary", "Operational alert"),
        description=alert_dict.get("description"),
        created_at_utc=now,
        acknowledged=False,
        resolved=False,
    )
    session.add(record)
    session.flush()
    return record


def acknowledge_operational_alert_in_db(
    session: Session,
    alert_id: str,
    acknowledged_by: str,
    agency_id: str = "default",
) -> bool:
    """Acknowledge an operational alert."""
    record = session.execute(
        select(OperationalAlertRecord).where(
            OperationalAlertRecord.agency_id == agency_id,
            OperationalAlertRecord.alert_id == alert_id,
        )
    ).scalar_one_or_none()

    if not record:
        return False

    record.acknowledged = True
    record.acknowledged_by = acknowledged_by
    record.acknowledged_at_utc = _utc_now()
    session.flush()
    return True


def get_operational_alerts_from_db(
    session: Session,
    agency_id: str | None = None,
    limit: int = 100,
    unresolved_only: bool = False,
) -> list[dict[str, Any]]:
    """Query operational alerts from the database."""
    stmt = select(OperationalAlertRecord)
    if agency_id:
        stmt = stmt.where(OperationalAlertRecord.agency_id == agency_id)
    if unresolved_only:
        stmt = stmt.where(OperationalAlertRecord.resolved == False)  # noqa: E712

    records = session.execute(
        stmt.order_by(OperationalAlertRecord.created_at_utc.desc()).limit(limit)
    ).scalars().all()

    return [
        {
            "id": r.id,
            "agency_id": r.agency_id,
            "alert_id": r.alert_id,
            "alert_type": r.alert_type,
            "severity": r.severity,
            "route_id": r.route_id,
            "vehicle_id": r.vehicle_id,
            "summary": r.summary,
            "description": r.description,
            "created_at_utc": r.created_at_utc.isoformat(),
            "acknowledged": r.acknowledged,
            "acknowledged_by": r.acknowledged_by,
            "acknowledged_at_utc": r.acknowledged_at_utc.isoformat() if r.acknowledged_at_utc else None,
            "resolved": r.resolved,
        }
        for r in records
    ]


def create_import_job(
    session: Session,
    job_id: str,
    job_type: str,
    source_filename: str,
    sha256_hash: str,
    agency_id: str = "default",
) -> ImportJobRecord:
    """Create a new import job tracking record."""
    job = ImportJobRecord(
        agency_id=agency_id,
        job_id=job_id,
        job_type=job_type,
        status="RUNNING",
        source_filename=source_filename,
        sha256=sha256_hash,
        records_processed=0,
        records_inserted=0,
        records_updated=0,
        started_at_utc=_utc_now(),
    )
    session.add(job)
    session.flush()
    return job


def complete_import_job(
    session: Session,
    job_id: str,
    status: str,
    records_processed: int = 0,
    records_inserted: int = 0,
    records_updated: int = 0,
    error_message: str | None = None,
) -> None:
    """Mark an import job as completed or failed."""
    job = session.execute(
        select(ImportJobRecord).where(ImportJobRecord.job_id == job_id)
    ).scalar_one_or_none()

    if job:
        job.status = status
        job.records_processed = records_processed
        job.records_inserted = records_inserted
        job.records_updated = records_updated
        job.error_message = error_message
        job.completed_at_utc = _utc_now()
        session.flush()
