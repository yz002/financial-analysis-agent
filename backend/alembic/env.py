"""
Alembic environment script. Reads DATABASE_URL from the environment (via
backend/db/base.py's get_database_url(), which itself loads backend/.env through
python-dotenv if present) rather than from alembic.ini -- so a real connection
string is never written to a committed file. Imports db.models so every ORM
class registers against Base.metadata, making it the valid autogenerate-diff
target for any future migration (the initial migration itself is hand-written,
not generated -- see 0001_initial_schema.py's own docstring).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from db.base import Base, get_database_url
from db import models  # noqa: F401 -- import registers all models on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
