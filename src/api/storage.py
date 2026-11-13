"""Atomic file stores used by the domain API modules.

Writes go to a sibling temporary file and are moved into place with
``os.replace`` so a crash never leaves a half-written store behind. Each store
also gets a process-wide lock so concurrent requests serialize instead of
interleaving.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(path: str | Path) -> threading.RLock:
    """Re-entrant per-path lock.

    Re-entrancy matters because ``append_jsonl`` takes the lock itself, and
    callers often hold it while appending a batch.
    """

    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def read_json(path: str | Path, default: Any) -> Any:
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError:
        return default
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def write_json_atomic(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(temporary, target)


def read_jsonl(path: str | Path) -> list[dict]:
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    records: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def append_jsonl(path: str | Path, record: dict, *, max_records: int | None = None) -> None:
    """Append one record, trimming the oldest entries past ``max_records``."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with lock_for(target):
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str))
            handle.write("\n")
        if max_records is not None:
            records = read_jsonl(target)
            if len(records) > max_records:
                trimmed = records[-max_records:]
                target.write_text(
                    "".join(json.dumps(item, sort_keys=True, default=str) + "\n" for item in trimmed),
                    encoding="utf-8",
                )


def write_jsonl(path: str | Path, records: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        "".join(json.dumps(item, sort_keys=True, default=str) + "\n" for item in records),
        encoding="utf-8",
    )
    os.replace(temporary, target)
