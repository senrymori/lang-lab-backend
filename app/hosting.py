"""The learning core (`lp`) for every learner: its page, API, WebSocket and tutor sessions, served here
under this server's sign-in. lp runs hosted (LP_HOSTED): a learner is an account here and their files
sit in data_dir/<learner id>; the models per mode and the limits come from settings;
how much a learner uses is capped by their lessons alone. `configure()` runs before anything imports lp — lp reads its environment at import."""
from __future__ import annotations

import logging
import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from starlette.requests import HTTPConnection

from . import abuse, auth
from .config import settings
from .db import sessionmaker
from .models import AuthSession, Learner, Spend, SpendAlert

log = logging.getLogger(__name__)
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


async def burst(lang: str, block: bool, count: int, seconds: int) -> None:
    """Speech asked for faster than a person speaks (lp.server._burst): a red flag in the admin's list of suspicious
    spend, or — far past anything human — the learner blocked, signed out everywhere, a lesson going now stopped."""
    learner = _learner.get()
    if learner is None:
        return
    now = datetime.now(timezone.utc)
    reason = (f"{count}+ распознаваний речи за {seconds} с — так быстро человек не говорит, это скрипт" if block
              else f"{count} распознаваний речи за {seconds} с — быстрее, чем говорит человек")
    async with sessionmaker()() as s:
        s.add(SpendAlert(learner_id=learner.id, sid="burst", lang=lang, usd_by_window={}, session_usd=0,
                         verdict="spam" if block else "flag", reason=reason))
        if block:
            await _block(s, learner, reason, now)
        await s.commit()
    if block:
        _stop_lessons(learner)


async def _block(s, learner: Learner, reason: str, now: datetime) -> None:
    """Blocked and signed out everywhere (the caller commits, then stops the lessons)."""
    await s.execute(update(Learner).where(Learner.id == learner.id).values(
        status="blocked", blocked_reason=f"авто: {reason}"[:500], blocked_at=now))
    await s.execute(update(AuthSession).where(AuthSession.learner_id == learner.id,
                                              AuthSession.revoked_at.is_(None)).values(revoked_at=now))


def _stop_lessons(learner: Learner) -> None:
    from lp import server
    for k, live in list(server.sessions.items()):
        if k.startswith(f"{learner.id}/") and live.task:
            live.task.cancel()


# OpenAI moderation's categories, as the admin reads them; self-harm is someone to help, so it never counts to a block
CATEGORY_RU = {"sexual": "сексуальное", "sexual/minors": "сексуальное с детьми", "harassment": "травля",
               "harassment/threatening": "угрозы", "hate": "ненависть", "hate/threatening": "ненависть с угрозами",
               "illicit": "незаконное", "illicit/violent": "незаконное с насилием", "self-harm": "самоповреждение",
               "self-harm/intent": "самоповреждение: намерение", "self-harm/instructions": "самоповреждение: инструкции",
               "violence": "насилие", "violence/graphic": "насилие в подробностях"}
WHERE_RU = {"chat": "в чате", "voice": "голосом", "answer": "в задании"}
# one flag short of a block, the lemur says so in the lesson
LAST_WARNING = "Ещё один подобный запрос — и лемуру придётся посадить тебя на корабль к пингвинам (бан)."


async def flagged(text: str) -> list[str]:
    """The categories OpenAI's moderation flags in this text (free; a failure flags nothing)."""
    from openai import AsyncOpenAI

    import lp.openai_tutor as ot
    try:
        r = await AsyncOpenAI(api_key=ot._key()).moderations.create(model=settings.moderation_model, input=text)
    except Exception as e:  # never silent: a key without the moderation model lets everything through
        log.warning("moderation failed, the message went unchecked: %s", str(e)[:300])
        return []
    res = r.results[0]
    if not res.flagged:
        return []
    return [k for k, on in res.categories.model_dump(by_alias=True).items() if on]


async def moderate(lang: str, text: str, where: str) -> None:
    """What the learner wrote or said (lp.server.learner_said): flagged — a red flag in the admin with the words;
    self-harm alone — noted for a human to look at, never blocked; one short of N — the lemur warns; the Nth — blocked."""
    learner = _learner.get()
    if learner is None:
        return
    cats = await flagged(text)
    if not cats:
        return
    care = all(c.startswith("self-harm") for c in cats)
    excerpt = text if len(text) <= 160 else text[:157] + "…"
    reason = f"{', '.join(CATEGORY_RU.get(c, c) for c in cats)} — {WHERE_RU.get(where, where)}: «{excerpt}»"
    now = datetime.now(timezone.utc)
    async with sessionmaker()() as s:
        alert = SpendAlert(learner_id=learner.id, sid="moderation", lang=lang, usd_by_window={}, session_usd=0,
                           verdict="care" if care else "flag", reason=reason[:500])
        s.add(alert)
        await s.flush()
        flags = await s.scalar(select(func.count()).select_from(SpendAlert).where(
            SpendAlert.learner_id == learner.id, SpendAlert.sid == "moderation", SpendAlert.verdict == "flag"))
        block = not care and flags >= settings.moderation_block_after
        warn = not care and flags == settings.moderation_block_after - 1
        if block:
            alert.verdict = "spam"
            await _block(s, learner, f"{flags}-е сообщение с недопустимым, "
                                     f"последнее: {reason}", now)
        await s.commit()
    if block:
        _stop_lessons(learner)
    elif warn:
        await _say_in_lessons(learner, LAST_WARNING)


async def _say_in_lessons(learner: Learner, text: str) -> None:
    """The lemur's line in every lesson of this learner going now (it stays in the lesson's log)."""
    from lp import server
    for k, live in list(server.sessions.items()):
        if k.startswith(f"{learner.id}/") and live.task and not live.task.done():
            await live.emit({"type": "tutor", "text": text})


def install():
    """lp's app with this server's hooks, ready to mount."""
    import lp.openai_tutor
    import lp.server
    lp.server.identify, lp.server.guard, lp.server.burst = identify, guard, burst
    lp.server.moderate = moderate
    lp.openai_tutor.check, lp.openai_tutor.book = check, book
    return lp.server.app
