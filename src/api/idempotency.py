"""Idempotency keys for write endpoints.

A client that retries a ``POST`` with the same ``Idempotency-Key`` header gets
the original response back instead of a duplicate record. Results are stored on
disk so a restart does not re-open a window for double writes.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Callable

from .storage import lock_for, read_json, write_json_atomic

IDEMPOTENCY_HEADER = "Idempotency-Key"
MAX_KEY_LENGTH = 200
DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_ENTRIES = 5_000


def normalize_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if len(key) > MAX_KEY_LENGTH:
        key = key[:MAX_KEY_LENGTH]
    return key


def fingerprint(scope: str, key: str) -> str:
    digest = hashlib.sha256(f"{scope}\x00{key}".encode("utf-8")).hexdigest()
    return digest[:32]


class IdempotencyStore:
    """Small JSON-backed map of ``scope+key`` to a stored response."""

    def __init__(self, path: str | Path, *, ttl_seconds: int = DEFAULT_TTL_SECONDS, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = lock_for(self.path)
        self._process_lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        payload = read_json(self.path, {})
        return payload if isinstance(payload, dict) else {}

    def _prune(self, entries: dict[str, dict], now: float) -> dict[str, dict]:
        fresh = {
            key: value
            for key, value in entries.items()
            if isinstance(value, dict) and now - float(value.get("stored_at", 0)) <= self.ttl_seconds
        }
        if len(fresh) > self.max_entries:
            ordered = sorted(fresh.items(), key=lambda item: float(item[1].get("stored_at", 0)))
            fresh = dict(ordered[-self.max_entries :])
        return fresh

    def lookup(self, scope: str, key: str | None) -> dict | None:
        if not key:
            return None
        slot = fingerprint(scope, key)
        with self._lock:
            entries = self._load()
            entry = entries.get(slot)
            if not isinstance(entry, dict):
                return None
            if time.time() - float(entry.get("stored_at", 0)) > self.ttl_seconds:
                return None
            return entry

    def record(self, scope: str, key: str | None, *, status_code: int, body: object) -> None:
        if not key:
            return
        slot = fingerprint(scope, key)
        with self._lock:
            entries = self._prune(self._load(), time.time())
            entries[slot] = {"scope": scope, "status_code": status_code, "body": body, "stored_at": time.time()}
            write_json_atomic(self.path, entries)

    def execute(
        self,
        scope: str,
        key: str | None,
        produce: Callable[[], tuple[int, object]],
    ) -> tuple[int, object, bool]:
        """Run ``produce`` once per key; return ``(status, body, replayed)``."""

        with self._process_lock:
            cached = self.lookup(scope, key)
            if cached is not None:
                return int(cached.get("status_code", 200)), cached.get("body"), True
            status_code, body = produce()
            self.record(scope, key, status_code=status_code, body=body)
            return status_code, body, False
