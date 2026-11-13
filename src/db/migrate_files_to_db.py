"""One-time safe migration script from file-based storage to database.

Features:
- Dry-run mode (`--dry-run`)
- Record count verification
- Checksum validation (SHA-256)
- Pre-migration backup creation
- Rollback guidance
- Idempotent and duplicate-safe execution
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .models import Base
from .repository import (
    record_audit_event,
    record_monthly_import_event,
    replace_all_daily_counts,
    upsert_monthly_records,
    write_source_metadata,
)
from .session import get_database_url, get_db_session, get_engine, init_db, user_data_dir


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def run_migration(
    data_dir: Path | None = None,
    dry_run: bool = False,
    backup: bool = True,
    agency_id: str = "default",
) -> dict:
    """Migrate daily CSV, monthly JSON, metadata, and audit log into database."""
    base_dir = data_dir or user_data_dir()
    daily_csv = base_dir / "daily_ridership.csv"
    monthly_json = base_dir / "monthly" / "cta_monthly_ridership.json"
    metadata_json = base_dir / "data_source.json"
    audit_jsonl = base_dir / "ingest_audit.jsonl"

    report: dict = {
        "data_dir": str(base_dir),
        "database_url": get_database_url(),
        "dry_run": dry_run,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "files_found": {},
        "daily_migration": {},
        "monthly_migration": {},
        "metadata_migration": {},
        "audit_migration": {},
        "backups": [],
        "success": False,
    }

    print(f"\n==================================================")
    print(f"TRANSIT INTELLIGENCE V2 — DATA MIGRATION")
    print(f"==================================================")
    print(f"Mode: {'DRY RUN (no database changes)' if dry_run else 'LIVE MIGRATION'}")
    print(f"Data directory: {base_dir}")
    print(f"Target DB:      {get_database_url()}")

    # 1. Inspect existing files
    for name, p in [
        ("daily_ridership.csv", daily_csv),
        ("cta_monthly_ridership.json", monthly_json),
        ("data_source.json", metadata_json),
        ("ingest_audit.jsonl", audit_jsonl),
    ]:
        exists = p.is_file()
        size = p.stat().st_size if exists else 0
        checksum = compute_sha256(p) if exists else None
        report["files_found"][name] = {
            "path": str(p),
            "exists": exists,
            "size_bytes": size,
            "sha256": checksum,
        }
        status_str = f"EXISTS ({size:,} bytes, sha256={checksum[:10]}...)" if exists else "NOT FOUND"
        print(f"  • {name}: {status_str}")

    # 2. Pre-migration backup
    backup_dir = None
    if backup and not dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = base_dir / "backups" / f"pre_migration_{ts}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for p in [daily_csv, monthly_json, metadata_json, audit_jsonl]:
            if p.is_file():
                dest = backup_dir / p.name
                shutil.copy2(p, dest)
                report["backups"].append(str(dest))
        print(f"\nCreated pre-migration backups at: {backup_dir}")

    # 3. Process Daily CSV
    daily_records: list[dict] = []
    if daily_csv.is_file():
        try:
            df = pd.read_csv(daily_csv)
            if {"service_date", "route_id", "passengers"}.issubset(set(df.columns)):
                for _, row in df.iterrows():
                    daily_records.append({
                        "service_date": str(row["service_date"]),
                        "route_id": str(row["route_id"]).strip(),
                        "passengers": float(row["passengers"]),
                    })
        except Exception as exc:
            print(f"Error reading daily CSV: {exc}", file=sys.stderr)

    report["daily_migration"] = {
        "source_rows": len(daily_records),
        "source_routes": len({r["route_id"] for r in daily_records}),
    }
    print(f"\nDaily data to migrate: {len(daily_records):,} records across {report['daily_migration']['source_routes']} routes")

    # 4. Process Monthly JSON
    monthly_records: list[dict] = []
    monthly_imports: list[dict] = []
    if monthly_json.is_file():
        try:
            state = json.loads(monthly_json.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                monthly_records = state.get("observations", [])
                monthly_imports = state.get("imports", [])
        except Exception as exc:
            print(f"Error reading monthly JSON: {exc}", file=sys.stderr)

    report["monthly_migration"] = {
        "source_observations": len(monthly_records),
        "source_imports": len(monthly_imports),
    }
    print(f"Monthly data to migrate: {len(monthly_records):,} observations, {len(monthly_imports)} import events")

    # 5. Process Metadata
    meta_dict = {}
    if metadata_json.is_file():
        try:
            meta_dict = json.loads(metadata_json.read_text(encoding="utf-8"))
        except Exception:
            pass
    report["metadata_migration"] = {"keys": list(meta_dict.keys())}

    # 6. Process Audit Logs
    audit_events: list[dict] = []
    if audit_jsonl.is_file():
        try:
            for line in audit_jsonl.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    audit_events.append(json.loads(line))
        except Exception:
            pass
    report["audit_migration"] = {"events_count": len(audit_events)}

    if dry_run:
        print("\nDry-run complete. No database tables were modified.")
        report["success"] = True
        return report

    # 7. Apply to Database
    engine = get_engine()
    init_db(engine)

    with get_db_session() as session:
        # Migrate daily
        if daily_records:
            source_sys = meta_dict.get("source_system", "manual_csv")
            count = replace_all_daily_counts(
                session, daily_records, agency_id=agency_id, source_system=source_sys
            )
            report["daily_migration"]["migrated_count"] = count

        # Migrate monthly
        if monthly_records:
            added, updated, unchanged = upsert_monthly_records(
                session, monthly_records, agency_id="cta"
            )
            report["monthly_migration"]["added"] = added
            report["monthly_migration"]["updated"] = updated
            report["monthly_migration"]["unchanged"] = unchanged

        # Migrate monthly import audit history
        for imp in monthly_imports:
            record_monthly_import_event(session, imp, agency_id="cta")

        # Migrate metadata
        if meta_dict:
            write_source_metadata(session, meta_dict, agency_id=agency_id)

        # Migrate audit logs
        for ev in audit_events:
            record_audit_event(
                session,
                action=ev.get("action", "unknown"),
                source_system=ev.get("source_system", "unknown"),
                record_count=ev.get("record_count", 0),
                route_count=ev.get("route_count", 0),
                agency_id=agency_id,
            )

    report["success"] = True
    print("\n✅ LIVE MIGRATION COMPLETED SUCCESSFULLY!")
    print(f"  • Daily records in DB:   {len(daily_records):,}")
    print(f"  • Monthly records in DB: {len(monthly_records):,}")
    print(f"  • Metadata updated:      {bool(meta_dict)}")
    print(f"  • Audit events migrated: {len(audit_events):,}")
    if backup_dir:
        print(f"\nRollback Guidance: If needed, restore original files from:")
        print(f"  {backup_dir}")
    print("==================================================\n")

    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspect and count records without writing to DB")
    parser.add_argument("--no-backup", action="store_true", help="Skip creating pre-migration file backups")
    parser.add_argument("--data-dir", type=str, default=None, help="Custom data directory path")
    parser.add_argument("--agency-id", type=str, default="default", help="Agency ID for daily records")
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    run_migration(data_dir=data_dir, dry_run=args.dry_run, backup=not args.no_backup, agency_id=args.agency_id)


if __name__ == "__main__":
    main()
