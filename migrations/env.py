"""Migrations run against the same database the app uses (settings or the dev Postgres in var/pg)."""
from alembic import context
from sqlalchemy import create_engine, pool

from app.config import settings
from app.db import database_url, stop_dev_db
from app.models import Base

target_metadata = Base.metadata


def run() -> None:
    if context.is_offline_mode():
        context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


def _dev_db_running() -> bool:
    from app.db import DEV_PG
    return (DEV_PG / "postmaster.pid").exists()


# dev: a migration run leaves the dev Postgres as it found it (a running app keeps its database)
was_running = settings.database_url or _dev_db_running()
run()
if not was_running:
    stop_dev_db()
