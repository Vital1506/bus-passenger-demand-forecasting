"""Validated, transaction-safe storage for monthly route-level boardings.

This module is intentionally independent of the daily forecasting pipeline.
It accepts the Chicago CTA monthly export and stores only monthly observations.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


SOURCE_COLUMNS = ("Month_Beginning", "route", "routename", "MonthTotal")
MONTHLY_COLUMNS = ("month_start", "route_id", "route_name", "boardings")
_NUMBER_WITH_GROUPING = re.compile(r"^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")
_STORE_LOCK = threading.RLock()
_STATE_VERSION = 1


class MonthlyDataError(ValueError):
    """Raised when an uploaded monthly ridership CSV is invalid."""


class MonthlyStoreError(RuntimeError):
    """Raised when the separate monthly store cannot be read or updated."""


@dataclass(frozen=True)
class MonthlyObservation:
    month_start: str
    route_id: str
    route_name: str
    boardings: str


def _month_start(value: str, row_number: int) -> str:
    text = value.strip()
    parsed = None
    for date_format in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, date_format).date()
            break
        except ValueError:
            continue
    if parsed is None:
        raise MonthlyDataError(f"Row {row_number}: Month_Beginning must be a valid MM/DD/YYYY or YYYY-MM-DD date")
    if parsed.day != 1:
        raise MonthlyDataError(f"Row {row_number}: Month_Beginning must be the first day of its month")
    return parsed.isoformat()


def _boardings(value: str, row_number: int) -> str:
    text = value.strip()
    if not text or not _NUMBER_WITH_GROUPING.fullmatch(text):
        raise MonthlyDataError(f"Row {row_number}: MonthTotal must be a nonnegative numeric boarding count")
    try:
        amount = Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise MonthlyDataError(f"Row {row_number}: MonthTotal is not numeric") from exc
    if not amount.is_finite() or amount < 0:
        raise MonthlyDataError(f"Row {row_number}: MonthTotal must be finite and nonnegative")
    # Canonical decimal text preserves numeric precision without float rounding.
    return format(amount.normalize(), "f")


def parse_monthly_csv(contents: bytes) -> list[MonthlyObservation]:
    """Validate and normalize the complete source file before any store access."""
    if not contents:
        raise MonthlyDataError("The uploaded CSV is empty")
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MonthlyDataError("The CSV must be UTF-8 encoded") from exc

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = next(reader)
    except StopIteration as exc:
        raise MonthlyDataError("The uploaded CSV has no header row") from exc
    except csv.Error as exc:
        raise MonthlyDataError(f"Malformed CSV header: {exc}") from exc

    if len(headers) != len(set(headers)):
        raise MonthlyDataError("CSV contains duplicate column headers")
    missing_headers = [name for name in SOURCE_COLUMNS if name not in headers]
    if missing_headers:
        raise MonthlyDataError(f"CSV is missing required headers: {', '.join(missing_headers)}")

    positions = {name: headers.index(name) for name in SOURCE_COLUMNS}
    observations: list[MonthlyObservation] = []
    seen: set[tuple[str, str]] = set()
    try:
        for row_number, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) != len(headers):
                raise MonthlyDataError(f"Row {row_number}: expected {len(headers)} columns but found {len(row)}")
            month_start = _month_start(row[positions["Month_Beginning"]], row_number)
            route_id = row[positions["route"]].strip()
            if not route_id:
                raise MonthlyDataError(f"Row {row_number}: route must not be blank")
            route_name = row[positions["routename"]].strip()
            boardings = _boardings(row[positions["MonthTotal"]], row_number)
            key = (month_start, route_id)
            if key in seen:
                raise MonthlyDataError(f"Row {row_number}: duplicate route-month key ({month_start}, {route_id})")
            seen.add(key)
            observations.append(MonthlyObservation(month_start, route_id, route_name, boardings))
    except csv.Error as exc:
        raise MonthlyDataError(f"Malformed CSV content: {exc}") from exc

    if not observations:
        raise MonthlyDataError("The CSV contains no monthly ridership records")
    return observations


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/").split("/")[-1].strip()
    if not normalized or normalized in {".", ".."}:
        return "upload.csv"
    return normalized[:255]


class MonthlyRidershipStore:
    """Monthly-only snapshot store with atomic observation/audit replacement."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read_state(self) -> dict:
        if not self.path.is_file():
            return {"schema_version": _STATE_VERSION, "observations": [], "imports": []}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MonthlyStoreError("The monthly dataset snapshot is unreadable; no changes were applied") from exc
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != _STATE_VERSION
            or not isinstance(state.get("observations"), list)
            or not isinstance(state.get("imports"), list)
        ):
            raise MonthlyStoreError("The monthly dataset snapshot has an unsupported or invalid format")
        return state

    def _atomic_write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(state, temporary_file, ensure_ascii=False, separators=(",", ":"))
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise MonthlyStoreError("The monthly dataset update could not be committed; the previous data remains active") from exc

    def import_csv(self, contents: bytes, original_filename: str, source_info: str | None = None) -> dict:
        # Complete validation occurs before creating/opening the active store.
        observations = parse_monthly_csv(contents)
        filename = _safe_filename(original_filename)
        source = source_info.strip() if source_info and source_info.strip() else None
        if source and len(source) > 500:
            raise MonthlyDataError("source_info must be 500 characters or fewer")
        uploaded_at = _utc_now()
        digest = hashlib.sha256(contents).hexdigest()
        with _STORE_LOCK:
            state = self._read_state()
            previous = {}
            for row in state["observations"]:
                try:
                    key = (row["month_start"], row["route_id"])
                    previous[key] = {"route_name": row["route_name"], "boardings": row["boardings"]}
                except (KeyError, TypeError) as exc:
                    raise MonthlyStoreError("The monthly dataset snapshot contains an invalid observation") from exc

            added = updated = unchanged = 0
            merged = dict(previous)
            for observation in observations:
                key = (observation.month_start, observation.route_id)
                value = {"route_name": observation.route_name, "boardings": observation.boardings}
                existing = previous.get(key)
                if existing is None:
                    added += 1
                elif existing != value:
                    updated += 1
                else:
                    unchanged += 1
                merged[key] = value

            final_count = len(merged)
            event = {
                "original_filename": filename,
                "uploaded_at_utc": uploaded_at,
                "source_info": source,
                "sha256": digest,
                "incoming_records": len(observations),
                "added_records": added,
                "updated_records": updated,
                "unchanged_records": unchanged,
                "final_record_count": final_count,
            }
            next_state = {
                "schema_version": _STATE_VERSION,
                "observations": [
                    {"month_start": month, "route_id": route, **merged[(month, route)]}
                    for month, route in sorted(merged)
                ],
                "imports": [*state["imports"], event],
            }
            self._atomic_write(next_state)
        return event

    def status(self) -> dict:
        """Return metadata only; a missing store is not created by this read."""
        result = {
            "dataset": "Chicago CTA monthly bus ridership",
            "granularity": "monthly",
            "storage_exists": self.path.is_file(),
            "record_count": 0,
            "route_count": 0,
            "first_month": None,
            "latest_month": None,
            "last_import": None,
        }
        if not self.path.is_file():
            return result
        with _STORE_LOCK:
            state = self._read_state()
        observations = state["observations"]
        routes = {row.get("route_id") for row in observations if isinstance(row, dict)}
        months = [row.get("month_start") for row in observations if isinstance(row, dict)]
        result.update({
            "record_count": len(observations),
            "route_count": len(routes),
            "first_month": min(months) if months else None,
            "latest_month": max(months) if months else None,
            "last_import": state["imports"][-1] if state["imports"] else None,
        })
        return result

    def observations(self) -> list[dict]:
        """Read normalized rows for internal use and verification."""
        with _STORE_LOCK:
            return list(self._read_state()["observations"])

