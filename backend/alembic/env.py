import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Make `app` importable when Alembic is invoked from backend/ (the normal case).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.db import models  # noqa: F401,E402  (registers models on Base.metadata)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override alembic.ini's sqlalchemy.url with the same ADMIN_DATABASE_URL the
# app itself uses, so migrations always target the deployment's real DB
# rather than a hardcoded placeholder that could silently drift from it.
config.set_main_option("sqlalchemy.url", settings.admin_database_url)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


#: Advisory-lock id serialising `alembic upgrade` against admin-db. Any fixed 64-bit
#: integer works -- Postgres advisory locks are a bare namespace with no registry -- so
#: it only has to be one no other component of this deployment reuses. Digits spell
#: "prometheus admin-db migrations".
_MIGRATION_LOCK_ID = 8317202504


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        # Serialise concurrent upgrades. docker-entrypoint.sh already runs this once
        # per container before uvicorn starts, but that does not cover two containers
        # booting together, or a developer running the CLI while one boots. Without the
        # lock each caller reads the same `alembic_version`, concludes the same
        # migrations are pending, and emits the same DDL -- the losers fail with
        # "relation already exists" or deadlock, and a failure here aborts the upgrade.
        #
        # Taken *before* run_migrations() so it is held before Alembic reads
        # `alembic_version`: a second caller then blocks first and reads the *updated*
        # revision, correctly finding nothing to do. The `_xact_` variant releases with
        # the transaction, so there is no unlock to leak on a failed migration.
        #
        # Unconditional: admin_database_url is Postgres-only (app/config.py), there is
        # no SQLite path. The id is interpolated, not bound -- it is a module constant,
        # and asyncpg does not accept the `%s` paramstyle anyway.
        connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_MIGRATION_LOCK_ID})")
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
