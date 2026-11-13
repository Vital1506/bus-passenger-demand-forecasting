"""Database connection, session management, and health checks for Transit Intelligence V2."""

from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base


def user_data_dir() -> Path:
    configured = os.getenv("BUS_FORECAST_DATA_DIR")
    if configured:
        p = Path(configured).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    candidates: list[Path] = []
    import sys
    if sys.platform == "win32":
        candidates.append(Path(os.getenv("LOCALAPPDATA", Path.home())) / "BusDemandForecast")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "BusDemandForecast")
    else:
        candidates.append(Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "bus-demand-forecast")

    # Workspace fallback for sandboxed execution or portability
    workspace_data = Path(__file__).resolve().parent.parent.parent / "data"
    candidates.append(workspace_data)

    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            test_file = cand / ".probe_write.tmp"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            return cand
        except OSError:
            continue

    workspace_data.mkdir(parents=True, exist_ok=True)
    return workspace_data


def get_database_url() -> str:
    """Resolve database URL from environment or default to local SQLite in DATA_DIR."""
    raw = os.getenv("DATABASE_URL", "").strip()
    if raw:
        # Render and Heroku use postgres:// which SQLAlchemy 2.0 deprecated in favor of postgresql://
        if raw.startswith("postgres://"):
            raw = "postgresql://" + raw[len("postgres://"):]
        return raw

    data_dir = user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "transit_intelligence.db"
    return f"sqlite:///{db_file.as_posix()}"


_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker | None = None


def get_engine(database_url: str | None = None) -> Engine:
    """Return a cached or newly configured SQLAlchemy Engine."""
    global _ENGINE, _SESSION_FACTORY
    url = database_url or get_database_url()

    if _ENGINE is not None and str(_ENGINE.url) == url:
        return _ENGINE

    if url.startswith("sqlite"):
        # SQLite configuration
        is_memory = ":memory:" in url
        engine_kwargs = {
            "connect_args": {"check_same_thread": False},
        }
        if is_memory:
            engine_kwargs["poolclass"] = StaticPool
        _ENGINE = create_engine(url, **engine_kwargs)
    else:
        # PostgreSQL / other production RDBMS configuration
        _ENGINE = create_engine(
            url,
            pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
            max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
            pool_pre_ping=True,
            pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
        )

    _SESSION_FACTORY = sessionmaker(autocommit=False, autoflush=False, bind=_ENGINE)
    return _ENGINE


def init_db(engine: Engine | None = None) -> None:
    """Create all defined database tables."""
    eng = engine or get_engine()
    Base.metadata.create_all(bind=eng)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a transactional DB session."""
    if _SESSION_FACTORY is None:
        get_engine()
    session = _SESSION_FACTORY()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Context manager for standalone scripts and CLI commands."""
    if _SESSION_FACTORY is None:
        get_engine()
    session = _SESSION_FACTORY()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_db_health(engine: Engine | None = None) -> dict:
    """Test connection and measure latency."""
    eng = engine or get_engine()
    start = time.perf_counter()
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "status": "healthy",
            "dialect": eng.dialect.name,
            "latency_ms": latency_ms,
            "connected": True,
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "status": "unhealthy",
            "dialect": eng.dialect.name,
            "latency_ms": latency_ms,
            "connected": False,
            "error": str(exc),
        }


def check_storage_health() -> dict:
    """Check storage accessibility, free space, and persistence indicators."""
    data_dir = user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    test_file = data_dir / ".storage_health_probe.tmp"
    can_write = False
    try:
        test_file.write_text("probe", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        can_write = True
    except OSError:
        pass

    total, used, free = 0, 0, 0
    try:
        usage = shutil.disk_usage(data_dir)
        total = usage.total
        used = usage.used
        free = usage.free
    except OSError:
        pass

    return {
        "data_directory": str(data_dir),
        "writable": can_write,
        "free_bytes": free,
        "total_bytes": total,
        "free_mb": round(free / (1024 * 1024), 1) if free else 0,
        "is_ephemeral_warning": bool(os.getenv("RENDER") and not os.getenv("BUS_FORECAST_DATA_DIR")),
    }


def get_migration_status(engine: Engine | None = None) -> dict:
    """Check database schema and table availability."""
    eng = engine or get_engine()
    from sqlalchemy import inspect
    inspector = inspect(eng)
    existing_tables = set(inspector.get_table_names())
    expected_tables = {table.name for table in Base.metadata.tables.values()}
    missing_tables = expected_tables - existing_tables
    return {
        "initialized": len(missing_tables) == 0 and len(existing_tables) > 0,
        "existing_table_count": len(existing_tables),
        "expected_table_count": len(expected_tables),
        "missing_tables": sorted(missing_tables),
        "tables": sorted(existing_tables),
    }
