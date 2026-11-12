"""Initial transit intelligence schema (baseline).

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-12

This baseline creates every table declared on ``src.schema.Base.metadata``.
Later revisions use explicit operations; this first one is intentionally
metadata-driven so the initial schema and the ORM can never drift apart.
"""

from __future__ import annotations

from alembic import op

from src import schema

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema.Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    schema.Base.metadata.drop_all(bind=op.get_bind())
