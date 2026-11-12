"""One-time migration of existing CSV/JSON stores into the database.

Behaviour:
* ``--dry-run`` reports what would be imported without writing anything.
* Source files are checksummed (SHA-256) and copied to a timestamped backup
  directory before the first write, unless ``--no-backup`` is given.
* Writes are idempotent upserts keyed by (route, date) and (route, month), so
  re-running the migration cannot duplicate records.
* Each run records a ``feed_imports`` row and an aggregate ``audit_events`` row.

Usage:
    python -m src.migrate_to_db --dry-run
    python -m src.migrate_to_db
    python -m src.migrate_to_db --no-backup
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import data_dir, get_engine, session_scope
from .monthly_data import load_monthly_store, monthly_store_path
from .schema import AuditEvent, DailyObservation, FeedImport, MonthlyObservation


def sha256_of(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _upsert(session, model, rows: list[dict], index_elements: list[str]) -> tuple[int, int]:
    """Insert or update rows; return (inserted, updated)."""
    if not rows:
        return 0, 0
    dialect = session.get_bind().dialect.name
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    keys = {(row[index_elements[0]], row[index_elements[1]]) for row in rows}
    existing = set()
    try:
        for record in session.execute(select(model)).scalars():
            existing.add((getattr(record, index_elements[0]), getattr(record, index_elements[1])))
    except Exception:  # table may be empty/new
        existing = set()
    inserted = len(keys - existing)
    updated = len(keys & existing)
    statement = insert(model).values(rows)
    update_columns = {
        column: getattr(statement.excluded, column)
        for column in rows[0]
        if column not in index_elements and column not in {"created_at"}
    }
    statement = statement.on_conflict_do_update(index_elements=index_elements, set_=update_columns)
    session.execute(statement)
    return inserted, updated


def _read_daily_csv(path: Path) -> tuple[list[dict], list[str]]:
    import pandas as pd

    if not path.exists():
        return [], ["daily CSV not found; skipped"]
    frame = pd.read_csv(path)
    issues: list[str] = []
    required = {"service_date", "route_id", "passengers"}
    missing = required - set(frame.columns)
    if missing:
        return [], [f"daily CSV missing columns: {', '.join(sorted(missing))}"]
    frame["service_date"] = pd.to_datetime(frame["service_date"], errors="coerce").dt.date
    frame["passengers"] = pd.to_numeric(frame["passengers"], errors="coerce")
    before = len(frame)
    frame = frame.dropna(subset=["service_date", "route_id", "passengers"])
    frame = frame[frame["passengers"] >= 0]
    dropped = before - len(frame)
    if dropped:
        issues.append(f"dropped {dropped} invalid daily rows")
    rows = [
        {
            "route_id": str(record.route_id).strip(),
            "service_date": record.service_date,
            "passengers": float(record.passengers),
            "source_system": "legacy_csv_migration",
            "provenance": "observed",
        }
        for record in frame.itertuples()
    ]
    return rows, issues


def _read_monthly_store(path: Path) -> tuple[list[dict], list[str]]:
    if not path.exists():
        return [], ["monthly store not found; skipped"]
    store = load_monthly_store(path)
    rows: list[dict] = []
    issues: list[str] = []
    for route_id, payload in (store.get("routes") or {}).items():
        route_name = payload.get("route_name") or route_id
        for month, record in (payload.get("records") or {}).items():
            try:
                if isinstance(record, dict):
                    boardings = record.get("boardings")
                    record_name = record.get("route_name") or route_name
                else:
                    boardings = record
                    record_name = route_name
                month_start = date.fromisoformat(f"{str(month)[:7]}-01")
                rows.append(
                    {
                        "route_id": str(route_id),
                        "route_name": str(record_name),
                        "month_start": month_start,
                        "boardings": float(boardings),
                        "source_system": "legacy_monthly_migration",
                        "provenance": "observed",
                    }
                )
            except (TypeError, ValueError):
                issues.append(f"skipped invalid monthly record {route_id}/{month}")
    return rows, issues


def _backup(paths: list[Path], target_dir: Path) -> list[str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = target_dir / f"pre-db-migration-{stamp}"
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for path in paths:
        if path.exists():
            shutil.copy2(path, destination / path.name)
            copied.append(str(destination / path.name))
    return copied


def run_migration(
    directory: Path | None = None,
    *,
    dry_run: bool = False,
    backup_dir: Path | None = None,
    make_backup: bool = True,
) -> dict:
    base = Path(directory) if directory else data_dir()
    daily_path = base / "daily_ridership.csv"
    monthly_path = monthly_store_path(base)

    daily_rows, daily_issues = _read_daily_csv(daily_path)
    monthly_rows, monthly_issues = _read_monthly_store(monthly_path)

    summary = {
        "dry_run": dry_run,
        "data_dir": str(base),
        "sources": {
            "daily_csv": {"path": str(daily_path), "sha256": sha256_of(daily_path), "rows": len(daily_rows)},
            "monthly_store": {"path": str(monthly_path), "sha256": sha256_of(monthly_path), "rows": len(monthly_rows)},
        },
        "issues": daily_issues + monthly_issues,
        "daily": {"inserted": 0, "updated": 0},
        "monthly": {"inserted": 0, "updated": 0},
        "backup": [],
        "import_ids": [],
    }
    if dry_run:
        summary["would_import"] = len(daily_rows) + len(monthly_rows)
        return summary

    if make_backup:
        summary["backup"] = _backup([daily_path, monthly_path], Path(backup_dir) if backup_dir else base / "backups")

    with session_scope() as session:
        daily_import = FeedImport(
            kind="daily_legacy_migration",
            source=str(daily_path),
            checksum=summary["sources"]["daily_csv"]["sha256"],
            record_count=len(daily_rows),
            status="completed" if daily_rows else "empty",
        )
        monthly_import = FeedImport(
            kind="monthly_legacy_migration",
            source=str(monthly_path),
            checksum=summary["sources"]["monthly_store"]["sha256"],
            record_count=len(monthly_rows),
            status="completed" if monthly_rows else "empty",
        )
        session.add_all([daily_import, monthly_import])
        session.flush()
        for row in daily_rows:
            row["import_id"] = daily_import.id
        for row in monthly_rows:
            row["import_id"] = monthly_import.id
        summary["daily"]["inserted"], summary["daily"]["updated"] = _upsert(
            session, DailyObservation, daily_rows, ["route_id", "service_date"]
        )
        summary["monthly"]["inserted"], summary["monthly"]["updated"] = _upsert(
            session, MonthlyObservation, monthly_rows, ["route_id", "month_start"]
        )
        daily_import.inserted_count = summary["daily"]["inserted"]
        daily_import.updated_count = summary["daily"]["updated"]
        monthly_import.inserted_count = summary["monthly"]["inserted"]
        monthly_import.updated_count = summary["monthly"]["updated"]
        session.add(
            AuditEvent(
                action="legacy_file_migration",
                source_system="csv+json",
                record_count=len(daily_rows) + len(monthly_rows),
                route_count=len({row["route_id"] for row in daily_rows + monthly_rows}),
                detail={"daily": summary["daily"], "monthly": summary["monthly"], "dry_run": False},
            )
        )
        session.flush()
        summary["import_ids"] = [daily_import.id, monthly_import.id]
    summary["database"] = {"dialect": get_engine().dialect.name}
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy CSV/JSON stores into the database")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args(argv)
    summary = run_migration(
        args.data_dir,
        dry_run=args.dry_run,
        backup_dir=args.backup_dir,
        make_backup=not args.no_backup,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
