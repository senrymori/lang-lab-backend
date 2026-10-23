"""Database: the URL from settings, or a dev Postgres of our own (pgserver) kept in var/pg — no Docker,
nothing installed system-wide. The app stops it on shutdown; `python -m app.devdb stop` stops it by hand."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import ROOT, settings

DEV_PG = ROOT / "var" / "pg"
_dev = {"server": None}


@lru_cache
def database_url() -> str:
    """postgresql+psycopg://… for SQLAlchemy (async and sync alike)."""
    url = settings.database_url
    if not url:
        import pgserver  # dev only
        DEV_PG.mkdir(parents=True, exist_ok=True)
        _dev["server"] = pgserver.get_server(str(DEV_PG), cleanup_mode="stop")
        url = _dev["server"].get_uri()
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def stop_dev_db() -> None:
    """Stop the dev Postgres if this process used it (never touches a configured database). Directly
    with pg_ctl: pgserver's own bookkeeping of who else uses it can leave it running."""
    if _dev["server"] is None:
        return
    _dev["server"] = None
    from .devdb import main as devdb
    devdb("stop")


@lru_cache
def engine():
    return create_async_engine(database_url(), pool_pre_ping=True)


@lru_cache
def sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False)
