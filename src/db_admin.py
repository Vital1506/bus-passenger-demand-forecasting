"""Database administration CLI: upgrade schema and report status.

Usage:
    python -m src.db_admin upgrade
    python -m src.db_admin status
"""

from __future__ import annotations

import json
import sys

from .db import alembic_config, check_database, migration_status, storage_health


def upgrade_database() -> dict:
    """Apply pending Alembic migrations. Returns the resulting revision state."""
    from alembic import command

    command.upgrade(alembic_config(), "head")
    return migration_status()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command_name = args[0] if args else "status"
    if command_name == "upgrade":
        state = upgrade_database()
        print(json.dumps({"database": check_database(), "migrations": state}, indent=2))
        return 0 if not state.get("pending") else 1
    if command_name == "status":
        print(
            json.dumps(
                {
                    "database": check_database(),
                    "migrations": migration_status(),
                    "storage": storage_health(),
                },
                indent=2,
            )
        )
        return 0
    print("usage: python -m src.db_admin [upgrade|status]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
