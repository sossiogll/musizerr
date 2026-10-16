from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
import sqlalchemy as sa
from sqlalchemy import pool

# Ensure `app/` is importable so we can pull in the SQLModel metadata
# regardless of where alembic is invoked from.
_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from sqlmodel import SQLModel  # noqa: E402

# Importing the models module is what registers every table on the
# shared SQLModel.metadata. Without this import autogenerate sees an
# empty metadata and proposes dropping every table.
import db.models  # noqa: F401,E402
from db.engine import POSTGRESQL, SQLITE, database_url, make_engine  # noqa: E402
from db.locks import advisory_xact_lock  # noqa: E402

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# The database is the application's own (the one resolution in db.engine):
# ``db.engine.alembic_config`` names it explicitly, the alembic command line
# resolves it like the app does.
_URL = config.attributes.get("database_url") or database_url()

target_metadata = SQLModel.metadata

# Serializes concurrent upgrades (two containers starting together).
_MIGRATION_LOCK = "alembic-migrations"

# Some revision ids are longer than the 32 characters Alembic gives the
# version column; PostgreSQL enforces it (SQLite does not).
_VERSION_TABLE = sa.Table(
    "alembic_version", sa.MetaData(), sa.Column("version_num", sa.String(255), primary_key=True),
)


def _context_options(dialect: str) -> dict:
    return {
        "target_metadata": target_metadata,
        # SQLite cannot ALTER COLUMN natively. Batch mode copies the table,
        # applies changes, and renames - required for any add/drop/alter that
        # goes beyond a plain ADD COLUMN. PostgreSQL alters in place.
        "render_as_batch": dialect == SQLITE,
    }


def run_migrations_offline() -> None:
    context.configure(
        url=_URL.render_as_string(hide_password=False),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_context_options(_URL.get_backend_name()),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # No statement timeout: a migration may rewrite a large table.
    connectable = make_engine(_URL, statement_timeout_ms=0, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, **_context_options(connection.dialect.name))
        with context.begin_transaction():
            if connection.dialect.name == POSTGRESQL:
                advisory_xact_lock(connection, _MIGRATION_LOCK)
            _VERSION_TABLE.create(connection, checkfirst=True)
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
