"""Dependencies shared by the domain routers.

The routers must reuse the application's existing authentication, RBAC, agency
scoping, rate limiting and auditing. Those live in :mod:`src.app`, so they are
resolved lazily here to avoid an import cycle (``app`` imports the routers).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Callable

from fastapi import Depends, Header, HTTPException, Request

from .. import security


def app_module():
    """Return the running application module (lazy to keep imports acyclic)."""

    from .. import app as app_module

    return app_module


def authorize(request: Request, authorization: str | None = Header(default=None)) -> security.Principal:
    """Authenticate, rate limit, enforce the path policy, and audit."""

    return app_module().authorize(request, authorization)


def require(permission: str) -> Callable[..., security.Principal]:
    """Permission-specific dependency layered on top of :func:`authorize`."""

    def dependency(principal: security.Principal = Depends(authorize)) -> security.Principal:
        workspace = app_module().security_registry()["workspace_agency"]
        try:
            security.require(principal, permission, workspace_agency=workspace)
        except security.SecurityError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return principal

    return dependency


def data_dir() -> Path:
    return app_module().DATA_DIR


def storage_path(name: str) -> Path:
    return data_dir() / name


def runner():
    """Process-wide job runner writing to the workspace data directory."""

    from .jobs import get_runner

    return get_runner(storage_path("api_jobs.json"))


def idempotency_store():
    from .idempotency import IdempotencyStore

    return IdempotencyStore(storage_path("api_idempotency.json"))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
