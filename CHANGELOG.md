# Changelog

All notable changes to the Bus Passenger Demand Forecasting platform will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - Phase 3: Persistence, Model Registry & Forecast Ledger

### Added
- **Model Registry Architecture (D2)**: Expanded `ModelArtifactRecord` / `ModelRegistryRecord` in `src/db/models.py` with `model_id`, `strategy`, `status` (`candidate`, `active`, `retired`), `hyperparameters_json`, and `package_versions_json`.
- **Forecast Ledger & Accuracy Tracking (D3)**: Enhanced `ForecastRecord` with `lead_day`, `seasonal_naive_predicted`, `actual_demand`, and `actual_observed_at_utc`. Added `compute_forecast_accuracy()` in `src/db/repository.py` and endpoint `GET /api/forecast/accuracy` reporting rolling WAPE and bias per route and per lead day vs seasonal naive.
- **Model Registry API (D2)**: Added endpoint `GET /api/model/registry` to inspect registered model builds, hyperparameters, and backtest metrics across lifecycle stages.
- **Alembic Database Migration (D1)**: Authored migration `3c4d5e6f7a8b_model_registry_and_forecast_ledger.py` and added automated column migration verification in `src/db/session.py`.
- **APIRouter Modularization (D4)**: Created modular domain APIRouters in `src/routers/` (`auth.py`, `ai.py`, `data.py`, `forecast.py`, `gtfs.py`, `operations.py`, `reports.py`, `realtime.py`) while maintaining 100% backward compatibility for all existing route decorators and test imports.

## [Phase 2] - Correctness, Forecasting Engine, and AI Reliability

### Added
- **Dynamic Model Versioning (B6)**: Replaced hardcoded version string with `get_model_version()` deriving dynamic semantic build identifier (`1.0.0+YYYYMMDDHHMM`) or `BUS_FORECAST_MODEL_VERSION`.
- **Per-Agency Timezone Support (B2)**: Added `get_agency_timezone()`, `get_current_local_time()`, and `get_current_hour_local()` supporting `zoneinfo.ZoneInfo` for agency-local operations, defaulting to `America/Chicago` or `BUS_FORECAST_TIMEZONE_<AGENCY>`.
- **Intraday Baseline Linking (B3)**: Linked `GET /api/forecast/realtime/{route_id}` and `GET /api/forecast/intraday` to trained model daily forecast outputs from `FORECAST_PATH` or `PUBLIC_DEMO_CACHE`; raises 404 with helpful diagnostic if no baseline exists.
- **Explicit Heuristic Interval Labeling (B4)**: Added `lower_90_heuristic` and `upper_90_heuristic` keys with `interval_method="heuristic_ratio"` in hourly intraday demand curves.
- **Empirical Interval Calibration Guard (B7)**: Replaced magic 50.0 fallback in `calibrate_empirical_intervals()` with explicit `ValueError("Insufficient data: cannot calibrate empirical error intervals without residuals")`.
- **AI Copilot Resilience & Token Expansion**: Increased token budget from 1000 to 2500 for reasoning models, added automatic retry on transient upstream disconnects / busy responses, and improved diagnostics for unexpected provider failures.
- **Cold-Start Splash Timeout Tolerance**: Extended `/health` probe timeout from 25s to 75s and `waitForServer()` deadline from 60s to 90s in `web/app.js` to eliminate false timeout aborts on Render free-tier cold starts.

## [Phase 1] - Security Hardening & Tenant Isolation

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
