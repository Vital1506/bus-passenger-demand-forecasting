# Changelog

All notable changes to the Bus Passenger Demand Forecasting platform will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - Phase 1: Security Hardening & Tenant Isolation

### Added
- **AuthenticatedPrincipal Security Identity**: Structured dataclass in `src/auth.py` encapsulating caller role, tenant agency binding (`principal.agency`), key identifier, and constant-time authentication attributes.
- **Explicit Route Authorization Map**: Comprehensive `ROUTE_AUTH_MAP` in `src/app.py` cataloging the minimum required role for every endpoint across Public, Analyst, Operator, and Administrator tiers.
- **Automated Route Auth Guard Matrix**: Integration test in `tests/test_auth_rbac.py` validating that every registered `/api/*` endpoint is mapped and fails closed on unauthenticated access.
- **SSRF Prevention & Webhook Host Validation**: Implemented `is_safe_webhook_url()` in `src/reporting.py` resolving DNS and blocking loopback (`127.0.0.1`, `::1`), link-local cloud metadata (`169.254.169.254`), private RFC 1918 networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), CGNAT, and enforcing `BUS_FORECAST_WEBHOOK_ALLOWLIST` and `follow_redirects=False`.
- **Benchmark Protection & Concurrency Lock**: Added `BENCHMARK_LOCK` and dataset SHA-256 caching (`_BENCHMARK_CACHE`) to `GET /api/model/benchmark` to prevent CPU exhaustion.
- **Rate Limiter Memory Management**: Implemented `evict_idle()` and sliding-window client capacity bounding (`max_clients=10000`) in `InMemoryRateLimiter` to eliminate memory leaks from high distinct client volumes.

### Changed
- **Constant-Time Token Validation**: Replaced vulnerable `!=` comparison with `hmac.compare_digest` across all configured API keys and environment configurations.
- **Fail-Closed Default Authentication**: Changed `resolve_principal()` to default unauthenticated callers to `PUBLIC` role unless explicit `BUS_FORECAST_DEV_MODE=1` is provided.
- **Agency Tenant Isolation**: Updated `verify_agency_isolation()` to fail closed (rejecting unassigned or mismatched agencies) and enforced via `enforce_agency_access()`.
- **Role Enforcement on Unprotected Endpoints**: Applied `require_role(...)` guards to `/api/reports/daily`, `/api/reports/daily/csv`, `/api/reports/capacity-risk`, `/api/model/benchmark`, `/api/realtime/demo-snapshot`, `/api/operations/*`, `/api/occupancy/*`, and `/api/simulator/evaluate`.
- **Backward Compatibility Preserved**: The public synthetic demo, GTFS catalog lookups, and AI Assistant remain accessible to public visitors without requiring an API key.
