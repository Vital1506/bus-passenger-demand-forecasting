# Transit Intelligence V2 — Database & Migration Guide

## 1. Overview

The platform uses a production-ready persistent data architecture supporting:
- **Local Development & Testing:** Zero-configuration SQLite (`data/transit_intelligence.db`)
- **Production & Cloud Deployments:** PostgreSQL configured through `DATABASE_URL`
- **Migration Engine:** Alembic version-controlled migrations
- **ORM:** SQLAlchemy 2.0 with connection pooling and transactional boundaries

## 2. Configuration (`DATABASE_URL`)

Set the database connection string via the `DATABASE_URL` environment variable:

```bash
# SQLite (default if unset)
DATABASE_URL="sqlite:///data/transit_intelligence.db"

# PostgreSQL (Production / Render)
DATABASE_URL="postgresql://user:password@host:5432/transit_db"
```

*Note: If a legacy `postgres://` URL is provided (common on Render and Heroku), the application automatically normalizes it to `postgresql://`.*

### Connection Pooling Options
- `DB_POOL_SIZE`: Pool size for persistent connections (default: `10`)
- `DB_MAX_OVERFLOW`: Overflow connections allowed above pool size (default: `20`)
- `DB_POOL_RECYCLE`: Seconds before connection recycling (default: `1800`)

## 3. Database Schema

The initial schema (`alembic/versions/13027f25dc62_initial_schema.py`) includes:

| Table | Purpose | Primary Indexes |
|---|---|---|
| `daily_passenger_records` | Route-day observed boardings | `(agency_id, service_date, route_id)`, `(agency_id, route_id, service_date)` |
| `monthly_passenger_records` | Monthly route observations | `(agency_id, month_start, route_id)`, `route_name` |
| `source_metadata` | Ingest provenance & record counts | `(agency_id, key)` |
| `data_audit_events` | Data change history & record counts | `(agency_id, timestamp_utc)` |
| `monthly_import_events` | Monthly CSV import logs with SHA-256 | `(agency_id, uploaded_at_utc)` |
| `model_artifact_records` | Model versions & backtest tracking | `(agency_id, model_version)` |
| `gtfs_feeds` | Imported GTFS schedule versions | `agency_id`, `imported_at_utc` |
| `gtfs_agencies` | Transit operators | `(feed_id, agency_id)` |
| `gtfs_routes` | Network route definitions | `(feed_id, route_id)`, `route_short_name` |
| `gtfs_stops` | Stops and geographic coordinates | `(feed_id, stop_id)`, `(feed_id, stop_lat, stop_lon)` |
| `gtfs_trips` | Trip runs and directional patterns | `(feed_id, trip_id)`, `(feed_id, route_id, service_id)` |
| `gtfs_stop_times` | Scheduled arrival and departures | `(feed_id, trip_id, stop_sequence)`, `(feed_id, stop_id)` |
| `gtfs_calendar` | Regular weekly operating calendars | `(feed_id, service_id)` |
| `gtfs_calendar_dates` | Holiday and exception dates | `(feed_id, service_id, date)` |
| `gtfs_shapes` | High-fidelity route alignment paths | `(feed_id, shape_id, shape_pt_sequence)` |

## 4. Running Migrations

To apply database migrations:

```powershell
# In the virtual environment
alembic upgrade head
```

To inspect migration history:

```powershell
alembic current
alembic history --verbose
```

To downgrade (rollback one revision):

```powershell
alembic downgrade -1
```

## 5. One-Time Data Migration from Files

For existing deployments with file-based CSV or JSON data:

```powershell
# 1. Perform a dry-run check (no database modifications)
python -m src.db.migrate_files_to_db --dry-run

# 2. Run live migration with automatic pre-migration backup
python -m src.db.migrate_files_to_db
```

The migration utility:
1. Computes SHA-256 hashes of all input files.
2. Creates a timestamped backup in `data/backups/pre_migration_<timestamp>/`.
3. Inserts daily passenger counts, monthly boardings, metadata, and audit logs.
4. Verifies record counts match source files.
5. Provides clear rollback guidance if needed.

## 6. Health & Readiness Monitoring Endpoints

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/ready` | GET | Public | Kubernetes/Render readiness probe (checks DB & storage) |
| `/api/db/health` | GET | Protected | Latency, dialect, and connection status |
| `/api/storage/health` | GET | Protected | Disk directory, write permissions, available MB |
| `/api/db/migrations` | GET | Protected | Initialized status and table availability |
