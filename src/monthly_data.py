"""Independent monthly passenger dataset storage.

Monthly boardings live in their own JSON document and are never read by the
daily forecasting pipeline (:mod:`src.forecasting`, :mod:`src.train`) or the
model artefacts. Storage layout::

    <BUS_FORECAST_DATA_DIR>/monthly/cta_monthly_ridership.json
    <BUS_FORECAST_DATA_DIR>/monthly/ingest_audit.jsonl

Only observed route-month records are stored. A month the source does not
report is absent from the store: nothing is zero-filled, interpolated, or
inferred. Boardings are monthly boarding counts, not unique passengers.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
DATASET_NAME = "cta_monthly_ridership"
REQUIRED_COLUMNS = ("month_start", "route_id", "route_name", "boardings")
MAX_ROUTE_ID_LENGTH = 64
MAX_ROUTE_NAME_LENGTH = 200
MAX_CORRECTION_HISTORY = 5000
DEFAULT_AUDIT_LIMIT = 12
_MONTH_START_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


class MonthlyDataError(ValueError):
    """Raised when a monthly CSV or stored dataset fails validation."""


def monthly_store_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "monthly" / f"{DATASET_NAME}.json"


def monthly_audit_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "monthly" / "ingest_audit.jsonl"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_store() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET_NAME,
        "updated_at_utc": None,
        "source": {},
        "totals": {"rows": 0, "routes": 0, "first_month": None, "last_month": None},
        "last_import": None,
        "corrections": [],
        "corrections_dropped": 0,
        "routes": {},
    }


def load_monthly_store(store_path: str | Path) -> dict:
    """Read the monthly store, returning an empty document when absent."""
    path = Path(store_path)
    if not path.exists():
        return empty_store()
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonthlyDataError("The stored monthly dataset could not be read") from exc
    if not isinstance(store, dict) or store.get("schema_version") != SCHEMA_VERSION:
        raise MonthlyDataError("The stored monthly dataset has an unsupported schema")
    store.setdefault("dataset", DATASET_NAME)
    store.setdefault("updated_at_utc", None)
    store.setdefault("source", {})
    store.setdefault("totals", {"rows": 0, "routes": 0, "first_month": None, "last_month": None})
    store.setdefault("last_import", None)
    store.setdefault("corrections", [])
    store.setdefault("corrections_dropped", 0)
    store.setdefault("routes", {})
    return store


def save_monthly_store(store_path: str | Path, store: dict) -> None:
    """Atomically replace the monthly store inside its own directory."""
    path = Path(store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store, indent=2, ensure_ascii=False)
    handle, temporary_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _clean_text(value, field: str, *, max_length: int) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise MonthlyDataError(f"{field} cannot be blank")
    if len(text) > max_length:
        raise MonthlyDataError(f"{field} cannot be longer than {max_length} characters")
    if any(ord(character) < 32 for character in text):
        raise MonthlyDataError(f"{field} cannot contain control characters")
    return text


def _parse_month_start(value) -> str:
    text = "" if value is None else str(value).strip()
    # strptime accepts unpadded values such as "2024-1-1"; require the exact shape.
    if not _MONTH_START_PATTERN.fullmatch(text):
        raise MonthlyDataError(f"month_start {text!r} is not a valid YYYY-MM-DD date")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise MonthlyDataError(
            f"month_start {text!r} is not a valid YYYY-MM-DD date"
        ) from None
    if parsed.day != 1:
        raise MonthlyDataError(
            f"month_start {text!r} must be the first day of a month (expected {parsed.year:04d}-{parsed.month:02d}-01)"
        )
    return f"{parsed.year:04d}-{parsed.month:02d}-01"


def _parse_boardings(value) -> int:
    text = "" if value is None else str(value).strip()
    if not text:
        raise MonthlyDataError("boardings cannot be blank")
    try:
        number = float(text)
    except ValueError:
        raise MonthlyDataError(f"boardings {text!r} is not a number") from None
    if not math.isfinite(number):
        raise MonthlyDataError(f"boardings {text!r} must be a finite number")
    if number < 0:
        raise MonthlyDataError(f"boardings {text!r} cannot be negative")
    if number != int(number):
        raise MonthlyDataError(f"boardings {text!r} must be a whole number of boardings")
    return int(number)


def validate_monthly_csv(contents: bytes) -> tuple[list[dict], str]:
    """Validate a monthly CSV and return normalized records plus the file hash.

    Raises :class:`MonthlyDataError` for unreadable, mis-shaped, duplicate, or
    out-of-range input. No record is repaired, dropped, or filled in.
    """
    if not contents:
        raise MonthlyDataError("The uploaded CSV is empty")
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise MonthlyDataError("The CSV must be UTF-8 encoded") from None
    reader = csv.DictReader(io.StringIO(text))
    headers = [str(name).strip() for name in (reader.fieldnames or [])]
    if not headers:
        raise MonthlyDataError("The CSV has no header row")
    if len(headers) != len(set(headers)):
        raise MonthlyDataError("The CSV has duplicate column names")
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise MonthlyDataError("Missing required CSV columns: " + ", ".join(missing))

    records: list[dict] = []
    seen: dict[tuple[str, str], int] = {}
    for line_number, row in enumerate(reader, start=2):
        if row.get(None):
            raise MonthlyDataError(f"Row {line_number} has more values than the header row")
        if all(row.get(column) in (None, "") for column in REQUIRED_COLUMNS):
            continue
        route_id = _clean_text(row.get("route_id"), "route_id", max_length=MAX_ROUTE_ID_LENGTH)
        month_start = _parse_month_start(row.get("month_start"))
        route_name = _clean_text(row.get("route_name"), "route_name", max_length=MAX_ROUTE_NAME_LENGTH)
        boardings = _parse_boardings(row.get("boardings"))
        key = (route_id, month_start)
        if key in seen:
            raise MonthlyDataError(
                f"Duplicate route-month record for route {route_id!r} in {month_start[:7]} "
                f"(rows {seen[key]} and {line_number}); resolve duplicates before uploading"
            )
        seen[key] = line_number
        records.append({
            "month_start": month_start,
            "route_id": route_id,
            "route_name": route_name,
            "boardings": boardings,
        })

    if not records:
        raise MonthlyDataError("The CSV contains a header but no data records")
    records.sort(key=lambda record: (record["month_start"], len(record["route_id"]), record["route_id"]))
    return records, hashlib.sha256(contents).hexdigest()


def _recompute_route(entry: dict) -> dict:
    months = entry.get("records") or {}
    if not months:
        entry.update({
            "route_name": entry.get("route_name") or "",
            "first_month": None,
            "last_month": None,
            "month_count": 0,
            "total_boardings": 0,
        })
        return entry
    ordered = sorted(months)
    entry["route_name"] = months[ordered[-1]]["route_name"]
    entry["first_month"] = ordered[0]
    entry["last_month"] = ordered[-1]
    entry["month_count"] = len(ordered)
    entry["total_boardings"] = sum(int(months[month]["boardings"]) for month in ordered)
    return entry


def _recompute_totals(store: dict) -> dict:
    routes = store.get("routes") or {}
    rows = 0
    months: list[str] = []
    for entry in routes.values():
        rows += len(entry.get("records") or {})
        if entry.get("first_month"):
            months.append(entry["first_month"])
        if entry.get("last_month"):
            months.append(entry["last_month"])
    store["totals"] = {
        "rows": rows,
        "routes": len(routes),
        "first_month": min(months) if months else None,
        "last_month": max(months) if months else None,
    }
    return store


def _record_correction(store: dict, *, route_id: str, month_start: str, field: str, previous, current, recorded_at: str) -> None:
    corrections = store.setdefault("corrections", [])
    corrections.append({
        "recorded_at_utc": recorded_at,
        "route_id": route_id,
        "month_start": month_start,
        "field": field,
        "previous": previous,
        "current": current,
    })
    overflow = len(corrections) - MAX_CORRECTION_HISTORY
    if overflow > 0:
        del corrections[:overflow]
        store["corrections_dropped"] = int(store.get("corrections_dropped", 0)) + overflow


def merge_monthly_records(
    store: dict,
    records: list[dict],
    *,
    file_name: str,
    file_sha256: str,
    uploaded_at_utc: str | None = None,
) -> tuple[dict, dict]:
    """Merge records into the store, preserving history and missing months."""
    uploaded_at = uploaded_at_utc or _now_utc()
    routes = store.setdefault("routes", {})
    inserted = updated = unchanged = 0
    corrections_before = len(store.get("corrections") or [])
    touched: set[str] = set()

    for record in records:
        route_id = record["route_id"]
        month_start = record["month_start"]
        entry = routes.get(route_id)
        if entry is None:
            entry = {"route_id": route_id, "route_name": record["route_name"], "records": {}}
            routes[route_id] = entry
        months = entry.setdefault("records", {})
        existing = months.get(month_start)
        if existing is None:
            months[month_start] = {"boardings": record["boardings"], "route_name": record["route_name"]}
            inserted += 1
            touched.add(route_id)
            continue
        changed = False
        if int(existing.get("boardings", -1)) != record["boardings"]:
            _record_correction(
                store,
                route_id=route_id,
                month_start=month_start,
                field="boardings",
                previous=existing.get("boardings"),
                current=record["boardings"],
                recorded_at=uploaded_at,
            )
            existing["boardings"] = record["boardings"]
            changed = True
        if existing.get("route_name") != record["route_name"]:
            _record_correction(
                store,
                route_id=route_id,
                month_start=month_start,
                field="route_name",
                previous=existing.get("route_name"),
                current=record["route_name"],
                recorded_at=uploaded_at,
            )
            existing["route_name"] = record["route_name"]
            changed = True
        if changed:
            updated += 1
            touched.add(route_id)
        else:
            unchanged += 1

    for route_id in touched:
        _recompute_route(routes[route_id])
    for route_id, entry in routes.items():
        if route_id not in touched:
            entry.setdefault("route_id", route_id)
            entry.setdefault("records", {})

    _recompute_totals(store)
    store["updated_at_utc"] = uploaded_at
    store["source"] = {
        "file_name": file_name,
        "file_sha256": file_sha256,
        "csv_rows": len(records),
        "uploaded_at_utc": uploaded_at,
    }
    summary = {
        "file_name": file_name,
        "file_sha256": file_sha256,
        "csv_rows": len(records),
        "inserted_records": inserted,
        "updated_records": updated,
        "unchanged_records": unchanged,
        "corrections_recorded": len(store.get("corrections") or []) - corrections_before,
        "row_count_after": int(store["totals"]["rows"]),
        "route_count_after": int(store["totals"]["routes"]),
        "first_month": store["totals"]["first_month"],
        "last_month": store["totals"]["last_month"],
        "idempotent": inserted == 0 and updated == 0,
        "uploaded_at_utc": uploaded_at,
        "missing_month_policy": "Absent route-months remain absent; no zero values are created and nothing is interpolated.",
    }
    store["last_import"] = summary
    return store, summary


def record_monthly_audit(audit_path: str | Path, summary: dict) -> None:
    """Append one aggregate-only audit event; never passenger-level rows."""
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp_utc": summary["uploaded_at_utc"],
        "action": "monthly_upload",
        "file_name": summary["file_name"],
        "file_sha256": summary["file_sha256"],
        "csv_rows": summary["csv_rows"],
        "inserted_records": summary["inserted_records"],
        "updated_records": summary["updated_records"],
        "unchanged_records": summary["unchanged_records"],
        "row_count_after": summary["row_count_after"],
        "route_count_after": summary["route_count_after"],
        "idempotent": summary["idempotent"],
    }
    with path.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(event, separators=(",", ":")) + "\n")


def read_monthly_audit(audit_path: str | Path, limit: int = DEFAULT_AUDIT_LIMIT) -> list[dict]:
    path = Path(audit_path)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max(1, limit):]
    except OSError:
        return []
    events: list[dict] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return list(reversed(events))


def build_monthly_status(store: dict) -> dict:
    totals = store.get("totals") or {}
    source = store.get("source") or {}
    corrections = store.get("corrections") or []
    return {
        "dataset": store.get("dataset", DATASET_NAME),
        "data_loaded": bool(totals.get("rows")),
        "row_count": int(totals.get("rows") or 0),
        "route_count": int(totals.get("routes") or 0),
        "first_month": totals.get("first_month"),
        "last_month": totals.get("last_month"),
        "updated_at_utc": store.get("updated_at_utc"),
        "source": {
            "file_name": source.get("file_name"),
            "file_sha256": source.get("file_sha256"),
            "csv_rows": source.get("csv_rows"),
            "uploaded_at_utc": source.get("uploaded_at_utc"),
        },
        "last_import": store.get("last_import"),
        "corrections_recorded": len(corrections),
        "corrections_dropped": int(store.get("corrections_dropped") or 0),
        "unit": "monthly boardings (not unique passengers)",
    }


def list_monthly_routes(store: dict) -> list[dict]:
    routes = store.get("routes") or {}
    ordered = sorted(routes, key=lambda route_id: (len(route_id), route_id))
    return [
        {
            "route_id": route_id,
            "route_name": routes[route_id].get("route_name"),
            "first_month": routes[route_id].get("first_month"),
            "last_month": routes[route_id].get("last_month"),
            "month_count": int(routes[route_id].get("month_count") or 0),
            "total_boardings": int(routes[route_id].get("total_boardings") or 0),
        }
        for route_id in ordered
    ]


def _name_history(entry: dict) -> list[dict]:
    segments: list[dict] = []
    for month in sorted(entry.get("records") or {}):
        name = entry["records"][month]["route_name"]
        if segments and segments[-1]["route_name"] == name:
            segments[-1]["last_month"] = month
            segments[-1]["month_count"] += 1
        else:
            segments.append({
                "route_name": name,
                "first_month": month,
                "last_month": month,
                "month_count": 1,
            })
    return segments


def get_monthly_route_history(store: dict, route_id: str) -> dict | None:
    """Return observed months for one route, or None when the route is unknown."""
    entry = (store.get("routes") or {}).get(str(route_id))
    if entry is None:
        return None
    records = entry.get("records") or {}
    ordered = sorted(records)
    return {
        "route_id": entry.get("route_id", str(route_id)),
        "route_name": entry.get("route_name"),
        "first_month": entry.get("first_month"),
        "last_month": entry.get("last_month"),
        "month_count": int(entry.get("month_count") or 0),
        "total_boardings": int(entry.get("total_boardings") or 0),
        "unit": "monthly boardings (not unique passengers)",
        "missing_month_policy": "Months absent from the source are absent here; they are not zero-filled.",
        "name_history": _name_history(entry),
        "records": [
            {
                "month_start": month,
                "boardings": int(records[month]["boardings"]),
                "route_name": records[month]["route_name"],
            }
            for month in ordered
        ],
    }


def ingest_monthly_csv(
    store_path: str | Path,
    audit_path: str | Path,
    contents: bytes,
    *,
    file_name: str,
) -> dict:
    """Validate, merge, persist, and audit one monthly CSV upload."""
    store = load_monthly_store(store_path)
    records, file_sha256 = validate_monthly_csv(contents)
    store, summary = merge_monthly_records(
        store,
        records,
        file_name=Path(str(file_name)).name or "upload.csv",
        file_sha256=file_sha256,
    )
    save_monthly_store(store_path, store)
    record_monthly_audit(audit_path, summary)
    return {"import": summary, "status": build_monthly_status(store)}
