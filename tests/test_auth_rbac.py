"""Unit tests for role-based access control, authentication, and rate limiting."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.auth import InMemoryRateLimiter, UserRole, require_role, resolve_role


def test_resolve_role_unrestricted_dev_mode(monkeypatch):
    monkeypatch.delenv("BUS_FORECAST_REQUIRE_AUTH", raising=False)
    role = resolve_role(None)
    assert role == UserRole.ADMINISTRATOR


def test_resolve_role_with_auth_enabled(monkeypatch):
    monkeypatch.setenv("BUS_FORECAST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BUS_FORECAST_OPERATOR_KEY", "operator-secret-123")
    monkeypatch.setenv("BUS_FORECAST_ADMIN_KEY", "admin-secret-456")

    # 1. No token -> PUBLIC
    assert resolve_role(None) == UserRole.PUBLIC
    assert resolve_role("Bearer invalid") == UserRole.PUBLIC

    # 2. Operator token -> OPERATOR
    assert resolve_role("Bearer operator-secret-123") == UserRole.OPERATOR

    # 3. Admin token -> ADMINISTRATOR
    assert resolve_role("Bearer admin-secret-456") == UserRole.ADMINISTRATOR


def test_unified_api_key_backward_compatibility(monkeypatch):
    monkeypatch.setenv("BUS_FORECAST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BUS_FORECAST_API_KEY", "single-master-key")

    assert resolve_role("Bearer single-master-key") == UserRole.ADMINISTRATOR


def test_require_role_enforcement(monkeypatch):
    monkeypatch.setenv("BUS_FORECAST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BUS_FORECAST_OPERATOR_KEY", "op-key")
    monkeypatch.setenv("BUS_FORECAST_ADMIN_KEY", "adm-key")

    op_dep = require_role(UserRole.OPERATOR)
    adm_dep = require_role(UserRole.ADMINISTRATOR)

    # Public caller
    with pytest.raises(HTTPException) as exc_401:
        op_dep(None)
    assert exc_401.value.status_code == 401

    # Operator caller accessing operator endpoint -> OK
    assert op_dep("Bearer op-key") == UserRole.OPERATOR

    # Operator caller accessing admin endpoint -> 403 Forbidden
    with pytest.raises(HTTPException) as exc_403:
        adm_dep("Bearer op-key")
    assert exc_403.value.status_code == 403

    # Admin caller accessing both -> OK
    assert op_dep("Bearer adm-key") == UserRole.ADMINISTRATOR
    assert adm_dep("Bearer adm-key") == UserRole.ADMINISTRATOR


def test_in_memory_rate_limiter():
    limiter = InMemoryRateLimiter(max_requests=3, window_seconds=10)

    assert limiter.check("client-1") is True
    assert limiter.check("client-1") is True
    assert limiter.check("client-1") is True
    # 4th request exceeds rate limit
    assert limiter.check("client-1") is False

    # Different client is unaffected
    assert limiter.check("client-2") is True


def test_auth_roles_api_endpoint(monkeypatch):
    import src.app as app_mod

    paths = {r.path for r in app_mod.app.routes}
    assert "/api/auth/roles" in paths

    monkeypatch.setenv("BUS_FORECAST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BUS_FORECAST_OPERATOR_KEY", "op-pass")

    pub_roles = app_mod.get_auth_roles(None)
    assert pub_roles["role"] == "public"
    assert pub_roles["is_authenticated"] is False

    op_roles = app_mod.get_auth_roles("Bearer op-pass")
    assert op_roles["role"] == "operator"
    assert op_roles["is_authenticated"] is True
    assert "read_realtime_telemetry" in op_roles["permissions"]

