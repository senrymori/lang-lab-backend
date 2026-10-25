"""LangLab server. Run (dev): uv run uvicorn app.main:app --reload

Its own routes (sign-in, /me, /health) come first; everything else is the learning core's app (`lp`:
its API and the tutor's WebSocket), mounted at the root under this server's sign-in. lp's page is not
served here: the interface ships inside the desktop app, a browser on this host gets nothing to open."""
from __future__ import annotations

import asyncio
import contextlib

from . import hosting

hosting.configure()  # before lp is imported anywhere

from fastapi import FastAPI, HTTPException  # noqa: E402
from sqlalchemy import text  # noqa: E402

from . import admin, auth, cleanup, google, intake  # noqa: E402
from .config import settings  # noqa: E402
from .db import engine, stop_dev_db  # noqa: E402


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # a mounted app's own lifespan doesn't run: what lp needs at start is done here
    from lp import server
    from lp.tutor import MEDIA
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    # Azure's voices are made in worker threads: their characters are counted back on this loop
    import lp.tts
    loop = asyncio.get_running_loop()
    lp.tts.on_azure = lambda chars: asyncio.run_coroutine_threadsafe(hosting.count("azure_chars", chars), loop)
    sweeper = asyncio.create_task(cleanup.every())
    yield
    sweeper.cancel()
    for s in server.sessions.values():
        if s.task:
            s.task.cancel()
    await engine().dispose()
    stop_dev_db()  # dev only: the Postgres from var/pg goes down with the server


app = FastAPI(title="LangLab", lifespan=lifespan)


@app.middleware("http")
async def admin_only_on_its_host(request, call_next):
    """The admin lives on its own host, behind Cloudflare Access; the learners' host has no /admin at all."""
    if settings.admin_host:
        from fastapi.responses import JSONResponse, RedirectResponse
        on_admin = request.url.hostname == settings.admin_host
        is_admin = request.url.path == "/admin" or request.url.path.startswith("/admin/")
        if is_admin and not on_admin:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if on_admin and not is_admin:
            return RedirectResponse("/admin/")
    return await call_next(request)
app.include_router(auth.router)
app.include_router(google.router)
app.include_router(admin.router)
app.include_router(intake.router)


@app.get("/health")
async def health():
    """The database answers and the learning core is importable."""
    async with engine().connect() as c:
        db = (await c.execute(text("select 1"))).scalar() == 1
    import lp.store  # noqa: F401  the core loads with this server's settings
    return {"ok": db, "db": db, "tutor": {"provider": settings.tutor_provider, "learn": settings.model_learn,
                                          "placement": settings.model_placement}}


@app.get("/")
@app.get("/manifest.webmanifest")
@app.get("/static/{rest:path}")
async def no_page(rest: str = ""):
    """lp's page and its files: only in the app (lang-lab-platform), never in a browser."""
    raise HTTPException(404)


if settings.admin_ui_dir and settings.admin_ui_dir.is_dir():  # after the admin API's routes, which win
    from fastapi.staticfiles import StaticFiles
    app.mount("/admin", StaticFiles(directory=settings.admin_ui_dir, html=True), name="admin")
app.mount("/", hosting.install())  # last: the routes above win
