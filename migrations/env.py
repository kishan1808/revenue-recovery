"""Alembic environment for the AI Revenue Recovery application.

Synchronous configuration:
- The database URL comes from app.config.settings.DATABASE_URL
  (the single source of truth). Nothing is read from os.environ here,
  and no literal URL template is used from alembic.ini.
- app.models is imported before target_metadata is read so every ORM
  model is registered on Base.metadata for autogenerate.
"""

from alembic import context
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  # imported for metadata registration side effect
from app.config import settings
from app.database import Base

config = context.config

if not settings.DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured. "
        "Set it via environment variable or .env file (see .env.example)."
    )

# Supply the URL programmatically from application settings so that
# alembic.ini never contains environment templates or credentials.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without connecting)."""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with a synchronous connection."""
    connectable = create_engine(
        settings.DATABASE_URL,
        poolclass=pool.NullPool,
    )

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
