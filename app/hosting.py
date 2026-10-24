"""The learning core (`lp`) for every learner: its page, API, WebSocket and tutor sessions, served here
under this server's sign-in. lp runs hosted (LP_HOSTED): a learner is an account here and their files
sit in data_dir/<learner id>; the models per mode and the limits come from settings;
how much a learner uses is capped by their lessons alone. `configure()` runs before anything imports lp — lp reads its environment at import."""
from __future__ import annotations

import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from starlette.requests import HTTPConnection

from . import abuse, auth
from .config import settings
from .db import sessionmaker
from .models import Learner, Spend

_learner: ContextVar[Learner | None] = ContextVar("langlab_learner", default=None)


def configure() -> None:
    env = {
        "LP_HOSTED": "1", "LP_USER": "nobody",
        "LP_DATA": str(settings.data_dir), "LP_RECORDINGS": str(settings.recordings_dir),
        "LP_TUTOR_PROVIDER": settings.tutor_provider, "LP_TUTOR_MODEL_LEARN": settings.model_learn,
        "LP_TUTOR_MODEL_PLACEMENT": settings.model_placement,
        "LP_AUDIO_MAX_SECONDS": str(settings.audio_max_seconds), "LP_UPLOAD_MAX_MB": str(settings.upload_max_mb),
        "LP_TEXT_MAX_CHARS": str(settings.text_max_chars),
        "LP_MESSAGES_PER_MINUTE": str(settings.messages_per_minute), "LP_UNANSWERED_MAX": str(settings.unanswered_max),
        "LP_HEAR_PER_MINUTE": str(settings.hear_per_minute), "LP_SAY_PER_MINUTE": str(settings.say_per_minute),
    }
    f = settings.openai_key_file
    key = settings.openai_key or (f.read_text().strip() if f.is_file() else "")
    if key:  # without it lp falls back to the developer's own key: fine on this Mac, never on the server
        env["OPENAI_API_KEY"] = key
    f = settings.gemini_key_file
    if key := settings.gemini_key or (f.read_text().strip() if f.is_file() else ""):
        env["GEMINI_API_KEY"] = key
    os.environ.update(env)


def _today():
    return datetime.now(timezone.utc).date()


async def spent_today(learner_id: uuid.UUID) -> float:
    async with sessionmaker()() as s:
        usd = (await s.execute(select(Spend.usd).where(Spend.learner_id == learner_id,
                                                       Spend.day == _today()))).scalar_one_or_none()
    return float(usd or 0)


# --- hooks into lp ---

async def identify(scope) -> bool:
    """lp's request or WebSocket → the signed-in learner, or False (401)."""
    import lp.core
    conn = HTTPConnection(scope)
    async with sessionmaker()() as s:
        found = await auth.signed_in(s, auth.token_of(conn))
        if found is None and scope["type"] == "websocket":
            found = await auth.by_ticket(s, conn.query_params.get("ticket", ""))
    if found is None or found[1].status != "active":
        return False
    learner = found[1]
    lp.core.use_learner(str(learner.id), learner.name)
    _learner.set(learner)
    return True


async def guard(lang: str, mode: str) -> None:
    """Before a lesson or a placement starts."""
    learner = _learner.get()
    if learner is None:
        raise HTTPException(401, "Войдите заново")
    if not auth.learner_json(learner)["access"]:
        raise HTTPException(403, "Доступ закончился — напишите нам, чтобы продлить")
    if mode in ("learn", "placement"):  # a lesson is used as it starts, a placement too; one atomic step, so two at once can't both pass
        async with sessionmaker()() as s:
            used = (await s.execute(update(Learner).where(Learner.id == learner.id,
                                                          Learner.lessons_used < Learner.lessons_total)
                                    .values(lessons_used=Learner.lessons_used + 1).returning(Learner.id))).first()
            await s.commit()
        if used is None:
            raise HTTPException(403, "Уроки закончились — напишите нам, чтобы добавить")


async def check(S) -> None:
    """Before every model call of a session: no spend cap (lessons are the limit), only spam (app/abuse.py)."""
    await abuse.watch(S)


async def book(S, usd: float) -> None:
    abuse.note(S, usd)
    stmt = insert(Spend).values(learner_id=uuid.UUID(S.user), day=_today(), usd=usd, calls=1)
    stmt = stmt.on_conflict_do_update(index_elements=[Spend.learner_id, Spend.day],
                                      set_={"usd": Spend.usd + stmt.excluded.usd, "calls": Spend.calls + 1})
    async with sessionmaker()() as s:
        await s.execute(stmt)
        await s.commit()


def install():
    """lp's app with this server's hooks, ready to mount."""
    import lp.openai_tutor
    import lp.server
    lp.server.identify, lp.server.guard = identify, guard
    lp.openai_tutor.check, lp.openai_tutor.book = check, book
    return lp.server.app
