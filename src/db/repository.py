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
    DailyPassengerRecord,
    DataAuditEvent,
    ModelArtifactRecord,
    MonthlyImportEvent,
    MonthlyPassengerRecord,
    SourceMetadata,
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
