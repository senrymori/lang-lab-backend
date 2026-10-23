"""Language Lab server. Run (dev): uv run uvicorn app.main:app --reload"""
from __future__ import annotations

import contextlib

from fastapi import FastAPI
from sqlalchemy import text

from .config import settings
from .db import engine, stop_dev_db


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    yield
    await engine().dispose()
    stop_dev_db()  # dev only: the Postgres from var/pg goes down with the server


app = FastAPI(title="Language Lab", lifespan=lifespan)


@app.get("/health")
async def health():
    """The database answers and the learning core is importable."""
    async with engine().connect() as c:
        db = (await c.execute(text("select 1"))).scalar() == 1
    import lp.store  # noqa: F401  the core loads with this server's settings
    return {"ok": db, "db": db, "tutor": f"{settings.tutor_provider}/{settings.tutor_model}"}
