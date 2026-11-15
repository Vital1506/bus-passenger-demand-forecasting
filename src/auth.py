"""Role-Based Access Control (RBAC), constant-time authentication, rate limiting, and tenant isolation."""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)


class UserRole(str, Enum):
    PUBLIC = "public"
    ANALYST = "analyst"
    OPERATOR = "operator"
    ADMINISTRATOR = "administrator"


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Authenticated security identity representing role, agency tenancy, and key identifier."""

    role: UserRole
    agency: str | None = None
    key_id: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.role != UserRole.PUBLIC

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, UserRole):
            return self.role == other
        if isinstance(other, AuthenticatedPrincipal):
            return (self.role, self.agency, self.key_id) == (other.role, other.agency, other.key_id)
        return False

    def __hash__(self) -> int:
        return hash((self.role, self.agency, self.key_id))


class InMemoryRateLimiter:
    """Sliding-window in-memory rate limiter per IP/client token with idle client eviction."""

    def __init__(self, max_requests: int = 120, window_seconds: int = 60, max_clients: int = 10000):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._history: dict[str, list[float]] = {}
        self._last_eviction: float = time.time()

    def evict_idle(self, now: float | None = None) -> int:
        """Evict clients with no requests in the current sliding window to prevent memory exhaustion."""
        now = now if now is not None else time.time()
        cutoff = now - self.window_seconds
        evicted = 0
        to_delete = []

        for client_id, timestamps in list(self._history.items()):
            active = [t for t in timestamps if t > cutoff]
            if not active:
                to_delete.append(client_id)
            else:
                self._history[client_id] = active

        for client_id in to_delete:
            del self._history[client_id]
            evicted += 1

        # Enforce maximum client map bound (LRU-style pruning of oldest entries)
        if len(self._history) > self.max_clients:
            excess = len(self._history) - self.max_clients
            for client_id in list(self._history.keys())[:excess]:
                del self._history[client_id]
                evicted += 1

        self._last_eviction = now
        return evicted

    def check(self, client_id: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds

        # Run sweep periodically (every 30 seconds)
        if now - self._last_eviction > 30.0:
            self.evict_idle(now)

        timestamps = self._history.get(client_id, [])
        active = [t for t in timestamps if t > cutoff]

        if len(active) >= self.max_requests:
            self._history[client_id] = active
            return False

        active.append(now)
        self._history[client_id] = active
        return True


_GLOBAL_RATE_LIMITER = InMemoryRateLimiter(max_requests=180, window_seconds=60)


def get_rate_limiter() -> InMemoryRateLimiter:
    return _GLOBAL_RATE_LIMITER


def _get_configured_keys() -> list[dict[str, Any]]:
    """Retrieve all configured API keys with roles and tenant agency bindings."""
    keys: list[dict[str, Any]] = []

    # 1. Multi-key JSON environment configuration (supports hashed or raw token structures)
    json_config = os.getenv("BUS_FORECAST_API_KEYS_JSON", "").strip()
    if json_config:
        try:
            parsed = json.loads(json_config)
            if isinstance(parsed, list):
                for entry in parsed:
                    if isinstance(entry, dict) and entry.get("key") and entry.get("role"):
                        try:
                            role = UserRole(entry["role"].lower())
                            keys.append({
                                "key": str(entry["key"]).strip(),
                                "role": role,
                                "agency": str(entry.get("agency", "")).strip() or None,
                                "key_id": str(entry.get("key_id", entry.get("name", "custom"))),
                            })
                        except ValueError:
                            logger.warning("Invalid role in BUS_FORECAST_API_KEYS_JSON entry: %s", entry.get("role"))
            elif isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(v, dict) and v.get("role"):
                        try:
                            role = UserRole(v["role"].lower())
                            keys.append({
                                "key": str(k).strip(),
                                "role": role,
                                "agency": str(v.get("agency", "")).strip() or None,
                                "key_id": str(v.get("key_id", "custom")),
                            })
                        except ValueError:
                            pass
        except Exception as exc:
            logger.warning("Could not parse BUS_FORECAST_API_KEYS_JSON: %s", exc)

    # 2. Standard discrete environment variables
    admin_key = (os.getenv("BUS_FORECAST_ADMIN_KEY") or "").strip()
    if admin_key:
        keys.append({"key": admin_key, "role": UserRole.ADMINISTRATOR, "agency": None, "key_id": "admin_env"})

    operator_key = (os.getenv("BUS_FORECAST_OPERATOR_KEY") or "").strip()
    operator_agency = (os.getenv("BUS_FORECAST_OPERATOR_AGENCY") or "CTA").strip() or None
    if operator_key:
        keys.append({"key": operator_key, "role": UserRole.OPERATOR, "agency": operator_agency, "key_id": "operator_env"})

    analyst_key = (os.getenv("BUS_FORECAST_ANALYST_KEY") or "").strip()
    analyst_agency = (os.getenv("BUS_FORECAST_ANALYST_AGENCY") or "CTA").strip() or None
    if analyst_key:
        keys.append({"key": analyst_key, "role": UserRole.ANALYST, "agency": analyst_agency, "key_id": "analyst_env"})

    # 3. Legacy master API key
    master_key = (os.getenv("BUS_FORECAST_API_KEY") or "").strip()
    if master_key:
        keys.append({"key": master_key, "role": UserRole.ADMINISTRATOR, "agency": None, "key_id": "master_env"})

    return keys


def resolve_principal(authorization: str | None = None) -> AuthenticatedPrincipal:
    """Resolve caller identity using constant-time digest comparisons and strict fail-closed defaults.
    
    Fail closed: If BUS_FORECAST_DEV_MODE is not explicitly enabled (1/true/yes),
    unauthenticated requests or invalid tokens are assigned PUBLIC role.
    """
    dev_mode = os.getenv("BUS_FORECAST_DEV_MODE", "").strip().lower() in {"1", "true", "yes"}
    if dev_mode and not authorization:
        return AuthenticatedPrincipal(role=UserRole.ADMINISTRATOR, agency="default", key_id="dev_mode")

    token = None
    if authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        elif len(parts) == 1:
            token = parts[0]

    if not token:
        return AuthenticatedPrincipal(role=UserRole.PUBLIC)

    # Validate against configured keys using constant-time comparison
    configured = _get_configured_keys()
    token_bytes = token.encode("utf-8")

    for entry in configured:
        candidate_bytes = entry["key"].encode("utf-8")
        if hmac.compare_digest(token_bytes, candidate_bytes):
            return AuthenticatedPrincipal(
                role=entry["role"],
                agency=entry["agency"],
                key_id=entry["key_id"],
            )

    return AuthenticatedPrincipal(role=UserRole.PUBLIC)


def resolve_role(authorization: str | None = None) -> UserRole:
    """Determine role from authorization header."""
    return resolve_principal(authorization).role


def require_role(min_role: UserRole):
    """FastAPI dependency enforcing minimum required role and returning authenticated principal."""

    def role_dependency(authorization: str | None = Header(default=None)) -> AuthenticatedPrincipal:
        principal = resolve_principal(authorization)
        role = principal.role

        role_hierarchy = {
            UserRole.PUBLIC: 0,
            UserRole.ANALYST: 1,
            UserRole.OPERATOR: 2,
            UserRole.ADMINISTRATOR: 3,
        }

        if role_hierarchy[role] < role_hierarchy[min_role]:
            if role == UserRole.PUBLIC:
                raise HTTPException(
                    status_code=401,
                    detail="Enter the API access key in app settings",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: Action requires '{min_role.value}' role; caller holds '{role.value}'",
            )
        return principal

    return role_dependency


def verify_agency_isolation(caller_role: UserRole, caller_agency: str | None, target_agency: str | None) -> bool:
    """Enforce agency-level tenant isolation. Administrators have global access; operators/analysts fail closed."""
    if caller_role == UserRole.ADMINISTRATOR:
        return True
    if not caller_agency or not target_agency:
        return False  # Fail closed: unassigned agency or missing target cannot cross tenants
    return caller_agency.strip().lower() == target_agency.strip().lower()


def enforce_agency_access(principal: AuthenticatedPrincipal | None, requested_agency: str | None) -> str:
    """Enforce agency tenant isolation:
    - Administrators have global access and can specify any agency (or default).
    - Non-administrators (operators, analysts) are strictly bound to principal.agency.
      Attempting to access a different agency raises 403 Forbidden.
    """
    if principal is None or principal.role == UserRole.ADMINISTRATOR:
        return requested_agency or "default"

    caller_agency = principal.agency or "default"
    if requested_agency and requested_agency.strip().lower() != caller_agency.strip().lower():
        raise HTTPException(
            status_code=403,
            detail=f"Forbidden: Principal agency '{caller_agency}' is not authorized to access agency '{requested_agency}'.",
        )
    return caller_agency


def authorize_ai_chat(authorization: str | None = Header(default=None)) -> AuthenticatedPrincipal:
    """Authorize AI chat requests using constant-time comparison, cleanly separating platform access from OpenRouter keys."""
    dev_mode = os.getenv("BUS_FORECAST_DEV_MODE", "").strip().lower() in {"1", "true", "yes"}
    if dev_mode and not authorization:
        return AuthenticatedPrincipal(role=UserRole.ADMINISTRATOR, agency="default", key_id="dev_mode")

    principal = resolve_principal(authorization)
    if authorization and not principal.is_authenticated:
        raise HTTPException(
            status_code=401,
            detail="Invalid API access key. Enter the correct API access key in app settings.",
        )

    require_auth = os.getenv("BUS_FORECAST_REQUIRE_AUTH", "").lower() in {"1", "true", "yes"}
    allow_public_ai = os.getenv("BUS_FORECAST_ALLOW_PUBLIC_AI", "true").lower() in {"1", "true", "yes"}

    if not principal.is_authenticated and require_auth and not allow_public_ai:
        raise HTTPException(
            status_code=401,
            detail="Enter the API access key in app settings to use the AI Copilot.",
        )

    token = None
    if authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        elif len(parts) == 1:
            token = parts[0]

    admin_key = (os.getenv("BUS_FORECAST_ADMIN_KEY") or "").strip()
    api_key = (os.getenv("BUS_FORECAST_API_KEY") or "").strip()
    op_key = (os.getenv("BUS_FORECAST_OPERATOR_KEY") or "").strip()

    if token:
        if admin_key and hmac.compare_digest(token, admin_key):
            return AuthenticatedPrincipal(role=UserRole.ADMINISTRATOR, agency=principal.agency, key_id="admin_env")
        if (api_key and hmac.compare_digest(token, api_key)) or (op_key and hmac.compare_digest(token, op_key)):
            return AuthenticatedPrincipal(role=UserRole.OPERATOR, agency=principal.agency, key_id="operator_env")

    return principal

