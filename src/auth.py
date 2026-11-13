"""Role-Based Access Control (RBAC), rate limiting, and audit logging."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from enum import Enum
from typing import Any

from fastapi import Header, HTTPException


class UserRole(str, Enum):
    PUBLIC = "public"
    OPERATOR = "operator"
    ADMINISTRATOR = "administrator"


class InMemoryRateLimiter:
    """Sliding-window in-memory rate limiter per IP/client token."""

    def __init__(self, max_requests: int = 120, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = defaultdict(list)

    def check(self, client_id: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        # Clean older timestamps
        self._history[client_id] = [t for t in self._history[client_id] if t > cutoff]
        if len(self._history[client_id]) >= self.max_requests:
            return False
        self._history[client_id].append(now)
        return True


_GLOBAL_RATE_LIMITER = InMemoryRateLimiter(max_requests=180, window_seconds=60)


def get_rate_limiter() -> InMemoryRateLimiter:
    return _GLOBAL_RATE_LIMITER


def resolve_role(authorization: str | None = None) -> UserRole:
    """Determine role from authorization header and environment configuration."""
    require_auth = os.getenv("BUS_FORECAST_REQUIRE_AUTH", "").lower() in {"1", "true", "yes"}
    if not require_auth:
        return UserRole.ADMINISTRATOR  # Unrestricted local developer mode

    token = None
    if authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        elif len(parts) == 1:
            token = parts[0]

    if not token:
        return UserRole.PUBLIC

    admin_key = os.getenv("BUS_FORECAST_ADMIN_KEY") or os.getenv("BUS_FORECAST_API_KEY")
    operator_key = os.getenv("BUS_FORECAST_OPERATOR_KEY") or os.getenv("BUS_FORECAST_API_KEY")

    if admin_key and token == admin_key:
        return UserRole.ADMINISTRATOR
    if operator_key and token == operator_key:
        return UserRole.OPERATOR

    return UserRole.PUBLIC


def require_role(min_role: UserRole):
    """FastAPI dependency enforcing minimum required role."""

    def role_dependency(authorization: str | None = Header(default=None)) -> UserRole:
        role = resolve_role(authorization)

        role_hierarchy = {
            UserRole.PUBLIC: 0,
            UserRole.OPERATOR: 1,
            UserRole.ADMINISTRATOR: 2,
        }

        if role_hierarchy[role] < role_hierarchy[min_role]:
            if role == UserRole.PUBLIC:
                raise HTTPException(
                    status_code=401,
                    detail="Authentication required: Provide a valid Bearer token for access",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: Action requires '{min_role.value}' role; caller holds '{role.value}'",
            )
        return role

    return role_dependency
