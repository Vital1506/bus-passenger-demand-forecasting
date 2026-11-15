"""Unit tests for role-based access control, authentication, and rate limiting."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.auth import (
    AuthenticatedPrincipal,
    InMemoryRateLimiter,
    UserRole,
    require_role,
    resolve_principal,
    resolve_role,
    verify_agency_isolation,
)


def test_resolve_role_unrestricted_dev_mode(monkeypatch):
    monkeypatch.setenv("BUS_FORECAST_DEV_MODE", "1")
    monkeypatch.delenv("BUS_FORECAST_REQUIRE_AUTH", raising=False)
    role = resolve_role(None)
    assert role == UserRole.ADMINISTRATOR


def test_resolve_role_fails_closed_by_default(monkeypatch):
    monkeypatch.delenv("BUS_FORECAST_DEV_MODE", raising=False)
    monkeypatch.delenv("BUS_FORECAST_REQUIRE_AUTH", raising=False)
    role = resolve_role(None)
    assert role == UserRole.PUBLIC


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


def test_in_memory_rate_limiter_idle_eviction_and_bounding():
    # 1. Idle client eviction
    limiter = InMemoryRateLimiter(max_requests=10, window_seconds=60, max_clients=5)
    limiter.check("active-client")
    limiter.check("idle-client-1")
    limiter.check("idle-client-2")

    assert len(limiter._history) == 3

    # Fast forward 61 seconds into future
    future_now = limiter._last_eviction + 61.0
    # Keep active-client active at future time
    limiter._history["active-client"] = [future_now - 10.0]

    evicted = limiter.evict_idle(now=future_now)
    assert evicted == 2
    assert "idle-client-1" not in limiter._history
    assert "idle-client-2" not in limiter._history
    assert "active-client" in limiter._history

    # 2. Maximum client capacity bounding
    small_limiter = InMemoryRateLimiter(max_requests=10, window_seconds=60, max_clients=3)
    for i in range(10):
        small_limiter._history[f"client-{i}"] = [future_now]
    small_limiter.evict_idle(now=future_now)
    assert len(small_limiter._history) <= 3


def test_agency_tenant_isolation(monkeypatch):
    import src.app as app_mod

    # Administrator can access any agency
    admin_principal = AuthenticatedPrincipal(role=UserRole.ADMINISTRATOR, agency="CTA")
    assert app_mod.enforce_agency_access(admin_principal, "PACE") == "PACE"
    assert app_mod.enforce_agency_access(admin_principal, None) == "default"

    # Operator matches assigned agency
    op_principal = AuthenticatedPrincipal(role=UserRole.OPERATOR, agency="CTA")
    assert app_mod.enforce_agency_access(op_principal, "cta") == "CTA"
    assert app_mod.enforce_agency_access(op_principal, "CTA") == "CTA"

    # Operator accessing different agency is forbidden
    with pytest.raises(HTTPException) as exc:
        app_mod.enforce_agency_access(op_principal, "PACE")
    assert exc.value.status_code == 403
    assert "not authorized to access agency 'PACE'" in exc.value.detail

    # verify_agency_isolation helper
    assert verify_agency_isolation(UserRole.ADMINISTRATOR, "CTA", "PACE") is True
    assert verify_agency_isolation(UserRole.OPERATOR, "CTA", "cta") is True
    assert verify_agency_isolation(UserRole.OPERATOR, "CTA", "PACE") is False
    assert verify_agency_isolation(UserRole.OPERATOR, None, "PACE") is False
    assert verify_agency_isolation(UserRole.OPERATOR, "CTA", None) is False


def test_benchmark_endpoint_admin_lock_and_cache(monkeypatch, tmp_path):
    import src.app as app_mod

    monkeypatch.delenv("BUS_FORECAST_DEV_MODE", raising=False)
    monkeypatch.setenv("BUS_FORECAST_ADMIN_KEY", "admin-secret-bench")
    monkeypatch.setenv("BUS_FORECAST_OPERATOR_KEY", "operator-secret-bench")

    client = TestClient(app_mod.app)

    # 1. Unauthenticated request -> 401
    resp = client.get("/api/model/benchmark")
    assert resp.status_code == 401

    # 2. Operator caller -> 403 (Requires Admin)
    resp = client.get("/api/model/benchmark", headers={"Authorization": "Bearer operator-secret-bench"})
    assert resp.status_code == 403

    # 3. Admin caller when lock is acquired -> 429
    with app_mod.BENCHMARK_LOCK:
        resp = client.get("/api/model/benchmark", headers={"Authorization": "Bearer admin-secret-bench"})
        assert resp.status_code == 429
        assert "Concurrent benchmarks are restricted" in resp.json()["detail"]


def test_route_auth_guard_matrix(monkeypatch):
    """Verify every registered API endpoint is mapped in ROUTE_AUTH_MAP and protected accordingly."""
    import src.app as app_mod

    monkeypatch.delenv("BUS_FORECAST_DEV_MODE", raising=False)
    monkeypatch.setenv("BUS_FORECAST_ADMIN_KEY", "adm-matrix-key")
    monkeypatch.setenv("BUS_FORECAST_OPERATOR_KEY", "op-matrix-key")

    client = TestClient(app_mod.app)

    for route in app_mod.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", set())
        if not path or not path.startswith("/api/"):
            continue

        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue

            # Check explicit mapping in ROUTE_AUTH_MAP
            matching_entry = None
            for (m, p), min_role in app_mod.ROUTE_AUTH_MAP.items():
                if m == method and p == path:
                    matching_entry = min_role
                    break

            assert matching_entry is not None, f"Endpoint {method} {path} missing from ROUTE_AUTH_MAP"

            # If protected endpoint (non-public), unauthenticated request must fail closed
            if matching_entry != UserRole.PUBLIC:
                # Avoid triggering validation 422 before auth by not sending bad body
                # The auth dependency runs before endpoint logic
                test_path = path.replace("{route_id}", "R10").replace("{trip_id}", "T1").replace("{vehicle_id}", "V1")
                if method == "GET":
                    res = client.get(test_path)
                    assert res.status_code in {401, 403}, f"Protected endpoint {method} {path} permitted unauthenticated call (status: {res.status_code})"
                elif method == "POST":
                    res = client.post(test_path)
                    assert res.status_code in {401, 403, 422}, f"Protected endpoint {method} {path} returned {res.status_code}"

