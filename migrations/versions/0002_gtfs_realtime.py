"""Add the GTFS-Realtime persistence tables.

Revision ID: 0002_gtfs_realtime
Revises: 0001_initial_schema
Create Date: 2026-11-13

The initial schema revision predates the GTFS-Realtime ORM models. This
incremental migration adds the five realtime tables for both new databases and
databases that have already applied the initial revision.
"""

from __future__ import annotations

from alembic import op

from src import schema

revision = "0002_gtfs_realtime"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


REALTIME_TABLES = [
    schema.GtfsRealtimeFeedSource.__table__,
    schema.GtfsRealtimePoll.__table__,
    schema.GtfsRealtimeVehiclePosition.__table__,
    schema.GtfsRealtimeTripUpdate.__table__,
    schema.GtfsRealtimeServiceAlert.__table__,
]


def upgrade() -> None:
    """Create missing realtime tables and their indexes/constraints."""
    schema.Base.metadata.create_all(
        bind=op.get_bind(),
        tables=REALTIME_TABLES,
        checkfirst=True,
    )


def downgrade() -> None:
    """Drop only the tables introduced by this revision."""
    schema.Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=list(reversed(REALTIME_TABLES)),
        checkfirst=True,
    )
