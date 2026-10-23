"""Language Lab server. Run (dev): uv run uvicorn app.main:app --reload

Its own routes (sign-in, /me, /health) come first; everything else is the learning core's app (`lp`:
the page, its API, the tutor's WebSocket), mounted at the root under this server's sign-in."""
from __future__ import annotations

import asyncio
import contextlib

from . import hosting

hosting.configure()  # before lp is imported anywhere

from fastapi import FastAPI  # noqa: E402
from sqlalchemy import text  # noqa: E402

from . import admin, auth, cleanup  # noqa: E402
from .config import settings  # noqa: E402
from .db import engine, stop_dev_db  # noqa: E402


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # a mounted app's own lifespan doesn't run: what lp needs at start is done here
    from lp import server
    from lp.tutor import MEDIA
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    sweeper = asyncio.create_task(cleanup.every())
    yield
    sweeper.cancel()
    for s in server.sessions.values():
        if s.task:
            s.task.cancel()
    await engine().dispose()
    stop_dev_db()  # dev only: the Postgres from var/pg goes down with the server


app = FastAPI(title="Language Lab", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(admin.router)


@app.get("/health")
async def health():
    """The database answers and the learning core is importable."""
    async with engine().connect() as c:
        db = (await c.execute(text("select 1"))).scalar() == 1
    import lp.store  # noqa: F401  the core loads with this server's settings
    return {"ok": db, "db": db, "tutor": {"provider": settings.tutor_provider, "learn": settings.model_learn,
                                          "placement": settings.model_placement}}


app.mount("/", hosting.install())  # last: the routes above win
