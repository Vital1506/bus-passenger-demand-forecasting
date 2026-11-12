"""Database engine, session handling, readiness and migration reporting.

Configuration comes from ``DATABASE_URL``. When it is unset the application
falls back to a local SQLite file inside ``BUS_FORECAST_DATA_DIR`` so local
development and tests stay simple. PostgreSQL URLs (``postgres://`` and
``postgresql://``) are normalized to the psycopg 3 driver.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

SQLITE_FILENAME = "bus_forecast.db"
_TRUTHY = {"1", "true", "yes", "on"}

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def data_dir() -> Path:
    configured = os.getenv("BUS_FORECAST_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "bus-demand-forecast"


def normalize_url(url: str) -> str:
    """Map provider-style PostgreSQL URLs onto the installed driver."""
    cleaned = url.strip()
    if cleaned.startswith("postgres://"):
        return "postgresql+psycopg://" + cleaned[len("postgres://") :]
    if cleaned.startswith("postgresql://"):
        return "postgresql+psycopg://" + cleaned[len("postgresql://") :]
    return cleaned


def database_url(directory: Path | None = None) -> str:
    configured = os.getenv("DATABASE_URL", "").strip()
    if configured:
        return normalize_url(configured)
    base = Path(directory) if directory else data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{base / SQLITE_FILENAME}"


def is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def engine_options(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}, "pool_pre_ping": True}
    return {
        "pool_size": int(os.getenv("BUS_FORECAST_DB_POOL_SIZE", "5")),
        "max_overflow": int(os.getenv("BUS_FORECAST_DB_MAX_OVERFLOW", "10")),
        "pool_timeout": float(os.getenv("BUS_FORECAST_DB_POOL_TIMEOUT", "30")),
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    }


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        url = database_url()
        _engine = create_engine(url, future=True, **engine_options(url))
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commit on success, roll back on failure."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Dispose cached engine state (used by tests and after config changes)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def check_database() -> dict:
    """Connectivity probe with a bounded wait; never raises."""
    started = time.perf_counter()
    url = database_url()
    safe_url = url.split("@")[-1] if "@" in url else url
    result = {
        "configured": bool(os.getenv("DATABASE_URL", "").strip()),
        "dialect": url.split(":", 1)[0].split("+", 1)[0],
        "target": safe_url,
        "reachable": False,
        "latency_ms": None,
        "error": None,
    }
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        result["reachable"] = True
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    except Exception as exc:  # pragma: no cover - depends on deployment
        result["error"] = exc.__class__.__name__
    return result


def alembic_config():
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url())
    return config


def migration_status() -> dict:
    """Return current and head revisions plus whether an upgrade is pending."""
    from alembic.script import ScriptDirectory

    status = {"current": None, "head": None, "pending": False, "error": None}
    try:
        config = alembic_config()
        status["head"] = ScriptDirectory.from_config(config).get_current_head()
    except Exception as exc:
        status["error"] = exc.__class__.__name__
        return status
    try:
        with get_engine().connect() as connection:
            value = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        status["current"] = value
    except Exception:
        status["current"] = None
    status["pending"] = status["current"] != status["head"]
    return status


def storage_health(directory: Path | None = None) -> dict:
    """Report writability, free space and key artifact presence/sizes."""
    base = Path(directory) if directory else data_dir()
    report = {
        "path": str(base),
        "exists": base.exists(),
        "writable": False,
        "free_bytes": None,
        "artifacts": {},
    }
    try:
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=base, prefix=".write-test-", delete=True):
            report["writable"] = True
    except OSError:
        report["writable"] = False
    try:
        report["free_bytes"] = shutil.disk_usage(base).free
    except OSError:
        report["free_bytes"] = None
    for name, path in {
        "daily_csv": base / "daily_ridership.csv",
        "monthly_store": base / "monthly" / "cta_monthly_ridership.json",
        "model": base / "artifacts" / "model.joblib",
        "backups": base / "backups",
    }.items():
        if path.exists():
            report["artifacts"][name] = {
                "present": True,
                "bytes": path.stat().st_size if path.is_file() else None,
            }
        else:
            report["artifacts"][name] = {"present": False, "bytes": None}
    return report
