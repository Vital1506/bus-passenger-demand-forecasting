"""Background jobs for long-running work.

Training, GTFS imports, and report builds can take far longer than a request
should. They are submitted here and executed on worker threads, so the FastAPI
event loop only ever stores a job record and returns immediately.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .storage import lock_for, read_json, write_json_atomic

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TERMINAL = {SUCCEEDED, FAILED}

DEFAULT_MAX_WORKERS = 2
DEFAULT_MAX_JOBS = 200
MAX_ERROR_LENGTH = 500


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class JobRunner:
    """Thread-pool backed runner with a durable job log."""

    def __init__(self, path: str | Path, *, max_workers: int = DEFAULT_MAX_WORKERS, max_jobs: int = DEFAULT_MAX_JOBS):
        self.path = Path(path)
        self.max_jobs = max_jobs
        self._lock = lock_for(self.path)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="api-job")
        self._sequence = 0

    # -- persistence -----------------------------------------------------
    def _load(self) -> list[dict]:
        payload = read_json(self.path, [])
        return payload if isinstance(payload, list) else []

    def _save(self, jobs: list[dict]) -> None:
        write_json_atomic(self.path, jobs[-self.max_jobs :])

    def _update(self, job_id: str, **changes: Any) -> dict | None:
        with self._lock:
            jobs = self._load()
            for job in jobs:
                if job.get("job_id") == job_id:
                    job.update(changes)
                    self._save(jobs)
                    return dict(job)
        return None

    # -- public API ------------------------------------------------------
    def submit(
        self,
        kind: str,
        run: Callable[..., Any],
        *,
        params: dict | None = None,
        submitted_by: str | None = None,
    ) -> dict:
        with self._lock:
            self._sequence += 1
            job = {
                "job_id": uuid.uuid4().hex[:16],
                "kind": kind,
                "status": QUEUED,
                "created_at": utc_now(),
                "queued_ms": self._sequence,
                "started_at": None,
                "finished_at": None,
                "submitted_by": submitted_by,
                "params": params or {},
                "result": None,
                "error": None,
            }
            jobs = self._load()
            jobs.append(job)
            self._save(jobs)
        self._executor.submit(self._execute, job["job_id"], run, dict(params or {}))
        return dict(job)

    def _execute(self, job_id: str, run: Callable[..., Any], params: dict) -> None:
        self._update(job_id, status=RUNNING, started_at=utc_now())
        try:
            result = run(**params)
        except Exception as exc:  # noqa: BLE001 - job failures are reported, not raised
            self._update(job_id, status=FAILED, finished_at=utc_now(), error=str(exc)[:MAX_ERROR_LENGTH])
            return
        self._update(job_id, status=SUCCEEDED, finished_at=utc_now(), result=result)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            for job in self._load():
                if job.get("job_id") == job_id:
                    return dict(job)
        return None

    def list(self, *, kind: str | None = None, status: str | None = None) -> list[dict]:
        with self._lock:
            jobs = self._load()
        selected = [
            job
            for job in jobs
            if (kind is None or job.get("kind") == kind) and (status is None or job.get("status") == status)
        ]
        selected.sort(key=lambda job: job.get("created_at") or "", reverse=True)
        return selected

    def summary(self) -> dict:
        jobs = self.list()
        counts = {QUEUED: 0, RUNNING: 0, SUCCEEDED: 0, FAILED: 0}
        for job in jobs:
            counts[job.get("status", QUEUED)] = counts.get(job.get("status", QUEUED), 0) + 1
        return {"total": len(jobs), "counts": counts, "workers": self._executor._max_workers}  # noqa: SLF001

    def wait(self, job_id: str, timeout: float = 30.0) -> dict | None:
        """Poll until a job is terminal. Used by tests and scripted callers."""

        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job and job.get("status") in TERMINAL:
                return job
            time.sleep(0.02)
        return self.get(job_id)


_RUNNERS: dict[str, JobRunner] = {}
_RUNNERS_LOCK = threading.Lock()


def shutdown_all(wait: bool = True) -> None:
    """Stop every cached runner. Used by tests and graceful shutdown."""

    with _RUNNERS_LOCK:
        runners = list(_RUNNERS.values())
        _RUNNERS.clear()
    for runner in runners:
        runner._executor.shutdown(wait=wait)  # noqa: SLF001


def get_runner(path: str | Path) -> JobRunner:
    key = str(path)
    with _RUNNERS_LOCK:
        runner = _RUNNERS.get(key)
        if runner is None:
            runner = JobRunner(path)
            _RUNNERS[key] = runner
        return runner
