from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from roomfinder import models  # noqa: F401 - registers the tables on Base.metadata
from roomfinder.config import get_settings
from roomfinder.db import Base, UTCDateTime

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def sync_database_url() -> str:
    return get_settings().sqlalchemy_url.replace("+aiosqlite", "")


def render_item(type_, obj, autogen_context):
    # UTCDateTime only converts values in Python; the column itself is a plain DATETIME
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=sync_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(sync_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
