
from alembic import context

from app.database import Base

config = context.config

target_metadata = Base.metadata


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_options={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    pass

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
