# API documentation

The backend is split into domain modules under `src/api/`, mounted on the existing
FastAPI application in `src/app.py`. Interactive docs: `GET /docs` (Swagger UI),
`GET /redoc`, machine-readable spec at `GET /openapi.json` (title
*Bus Passenger Demand Forecast*, version `0.2.0`).

Existing endpoints (`/api/status`, `/api/train`, `/api/forecast`, `/api/gtfs/*`,
`/api/reports/*`, `/api/notifications/*`, `/api/monthly-data/*`, `/api/security/*`)
are unchanged. The domains below are additive.

## Conventions

**Authentication and authorization.** Every endpoint depends on the existing
`authorize` dependency (`src/app.py`), which resolves the bearer key, applies the
rate limit, enforces a permission from `SECURITY_POLICY`, scopes the request to
the workspace agency, and writes an audit record. Domain endpoints layer an
explicit permission on top so a permission can never be inferred from the path
alone. `BUS_FORECAST_REQUIRE_AUTH` unset means a local admin principal (desktop
mode); set it to `1`/`true` for hosted deployments.

**Pagination.** Every list endpoint accepts `page` (1-based, ≥1) and `page_size`
(1–200, default 50) and returns:

```json
{ "items": [], "page": 1, "page_size": 50, "total": 0, "pages": 0,
  "has_next": false, "has_previous": false }
```

**Idempotency.** Write endpoints accept an optional `Idempotency-Key` header
(≤200 chars). The first successful call is recorded; a retry with the same key
returns the stored body and `Idempotent-Replayed: true` instead of writing twice.
Keys expire after 24 h and the store is capped. Store writes use a temporary
file plus `os.replace`, so a crash cannot leave a half-written store.

**Background jobs.** Anything that can take longer than a request lives behind a
job: training, forecast refresh, GTFS import, report builds. The endpoint returns
`202` with a job record; poll the returned `job_id`. Workers are threads in the
same process (`src/api/jobs.py`), so the FastAPI event loop is never blocked.
Jobs persist to `api_jobs.json` in the data directory and are capped at 200.

**Data safety.** Ingested data is aggregate only. Passenger identifiers are
rejected by `extra="forbid"` models and by an explicit field screen against
`security.PASSENGER_IDENTIFIER_FIELDS`.

## Domains

### Authentication — `src/api/auth.py` (`/api/auth`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| GET | `/api/auth/capabilities` | `data:read` | Caller principal, permission list, role matrix, path policy, rate limit, `auth_required` |
| GET | `/api/auth/audit` | `security:admin` | Paginated, redacted audit trail (filters: `action`, `outcome`) |

### Transit network operations — `src/api/network.py` (`/api/network`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| GET | `/api/network/overview` | `gtfs:read` | Agency coverage: routes, months, totals, daily records, labels |
| GET | `/api/network/routes` | `gtfs:read` | Paginated route coverage (`q` substring filter) |
| GET | `/api/network/routes/{route_id}/coverage` | `gtfs:read` | Month-by-month history, gaps, name changes |

### GTFS static ingestion — `src/api/gtfs_static_api.py` (`/api/ingestion/gtfs-static`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/api/ingestion/gtfs-static/validate` | `gtfs:write` | Parse a feed and summarize it; no database write |
| POST | `/api/ingestion/gtfs-static/import` | `gtfs:write` | Queue an import (job `gtfs.static.import`) |
| GET | `/api/ingestion/gtfs-static/jobs` | `gtfs:read` | Paginated import jobs |
| GET | `/api/ingestion/gtfs-static/jobs/{job_id}` | `gtfs:read` | One import job |
| GET | `/api/ingestion/gtfs-static/status` | `gtfs:read` | Database reachability and latest import |

Uploads are screened by `security.safe_upload_name`; the body limit is 200 MB.

### GTFS-Realtime ingestion — `src/api/gtfs_realtime.py` (`/api/ingestion/gtfs-realtime`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/api/ingestion/gtfs-realtime/snapshots` | `gtfs:write` | Store a snapshot (deduplicated by content checksum) |
| GET | `/api/ingestion/gtfs-realtime/snapshots` | `gtfs:read` | Paginated snapshots (`agency` filter) |
| GET | `/api/ingestion/gtfs-realtime/status` | `gtfs:read` | Snapshot count, latest, stale flag |
| DELETE | `/api/ingestion/gtfs-realtime/snapshots` | `security:admin` | Clear stored snapshots |

### Passenger observation ingestion — `src/api/observations.py` (`/api/ingestion/observations`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/api/ingestion/observations` | `data:write` | Append aggregate route/day counts (up to 5 000 rows) |
| GET | `/api/ingestion/observations` | `data:read` | Paginated rows (`route_id`, `date_from`, `date_to`) |
| GET | `/api/ingestion/observations/summary` | `data:read` | Totals, route count, first/latest service date |

Repeated `route_id`+`service_date` pairs are reported under `duplicates` rather
than stored twice.

### Forecasting — `src/api/forecasting_api.py` (`/api/forecasting`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/api/forecasting/jobs` | `model:train` | Queue `train` or `forecast` (`horizon`, `folds`, `days`, `missing_days`) |
| GET | `/api/forecasting/jobs` | `forecast:run` | Paginated forecasting jobs |
| GET | `/api/forecasting/jobs/{job_id}` | `forecast:run` | One job |
| GET | `/api/forecasting/jobs/summary/status` | `forecast:run` | Counts by status |

The worker holds `PIPELINE_LOCK` for the duration so a job cannot interleave with
`POST /api/data/upload` or `POST /api/train`.

### Weather integration — `src/api/weather.py` (`/api/weather`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/api/weather/observations` | `data:write` | Append coarse station observations (up to 1 000) |
| GET | `/api/weather/observations` | `data:read` | Paginated observations (`station_id`, `observed_from`, `observed_to`) |
| GET | `/api/weather/status` | `data:read` | Provider configuration and storage summary |
| DELETE | `/api/weather/observations` | `security:admin` | Clear stored observations |

Provider selection is configuration only: `BUS_FORECAST_WEATHER_PROVIDER` and
`BUS_FORECAST_WEATHER_TOKEN`. The token value is never returned, only
`token_present`.

### Occupancy calculation — `src/api/occupancy.py` (`/api/occupancy`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| GET | `/api/occupancy/projection` | `reports:read` | Utilisation per route for the next `days` |
| GET | `/api/occupancy/routes` | `reports:read` | Paginated projection rows, highest utilisation first |
| GET | `/api/occupancy/assumptions` | `reports:read` | Stored capacity assumptions and derived daily capacity |

Derived, never observed: `daily_capacity = seats_per_vehicle × (peak_hours /
peak_headway_minutes + off_peak_hours / off_peak_headway_minutes)`. Without a
forecast or assumptions the response is `available: false` with an
`unavailable` label and a reason.

### Alert generation — `src/api/alerts.py` (`/api/alerts`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/api/alerts/generate` | `notifications:write` | Evaluate thresholds, store detected alerts |
| GET | `/api/alerts` | `notifications:read` | Paginated alerts (`severity`, `code`) |
| GET | `/api/alerts/summary` | `notifications:read` | Counts by severity and latest detection |

Detection lives here; delivery (email, webhook, in-app) stays in
`src/notifications.py`. Alerts are aggregate-only.

### Reporting — `src/api/reporting_api.py` (`/api/reporting`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| GET | `/api/reporting/catalog` | `reports:read` | Report definitions, availability, notice |
| POST | `/api/reporting/runs` | `reports:read` | Queue a report build (job `report.build`) |
| GET | `/api/reporting/runs` | `reports:read` | Paginated build history |
| GET | `/api/reporting/runs/{job_id}` | `reports:read` | One build |

Generated CSVs land in `<data dir>/reports/`.

### Data source management — `src/api/sources.py` (`/api/sources`)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| GET | `/api/sources` | `gtfs:read` | Registered sources (`kind`, `enabled` filters) |
| GET | `/api/sources/{source_id}` | `gtfs:read` | One source |
| POST | `/api/sources` | `gtfs:write` | Register/replace a source (idempotent) |
| PATCH | `/api/sources/{source_id}` | `gtfs:write` | Toggle, retune cadence, or `mark_success` |
| DELETE | `/api/sources/{source_id}` | `gtfs:write` | Remove a source |
| GET | `/api/sources/{source_id}/freshness` | `gtfs:read` | Freshness label (`imported`/`stale`/`unavailable`) |

## Examples

```bash
# capability matrix
curl -s localhost:3000/api/auth/capabilities | jq .roles

# paginated observations
curl -s "localhost:3000/api/ingestion/observations?page=2&page_size=100" | jq '.total, .pages'

# idempotent write: the second call replays the first response
curl -s -X POST localhost:3000/api/sources \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cta-daily-v1' \
  -d '{"source_id":"cta-daily","name":"CTA daily counts","kind":"observations","max_age_minutes":1440}'

# queue training, then poll the job
curl -s -X POST localhost:3000/api/forecasting/jobs \
  -H 'Content-Type: application/json' -d '{"kind":"train","horizon":7,"folds":4}'
curl -s localhost:3000/api/forecasting/jobs/<job_id>
```

## Environment

| Variable | Effect |
| --- | --- |
| `BUS_FORECAST_REQUIRE_AUTH` | `1`/`true` requires a bearer key; unset = local admin principal |
| `BUS_FORECAST_AGENCY` | Workspace agency code |
| `BUS_FORECAST_RATE_LIMIT_PER_MINUTE` | Per-principal request ceiling (default 240) |
| `BUS_FORECAST_CORS_ORIGINS` | Comma-separated allowed origins; wildcard is refused |
| `BUS_FORECAST_DATA_DIR` | Where API stores, jobs and idempotency records live |
| `BUS_FORECAST_WEATHER_PROVIDER`, `BUS_FORECAST_WEATHER_TOKEN` | Optional weather collector |

## Boundaries

* Jobs run in-process. A multi-worker deployment would need a shared queue
  (the `JobRunner` interface is the seam for that).
* `JobRunner` and `IdempotencyStore` are per-process; their files are safe for
  one server process, not for concurrent writers across replicas.
* Weather and GTFS-Realtime stores are bounded append-only logs, not a database.
