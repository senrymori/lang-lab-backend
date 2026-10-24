"""Spam that burns tokens on purpose, caught inside one live lesson. Money alone never stops anyone: a
learner may take four lessons in a row. But when one session spends fast (settings.spike_windows: e.g.
$2 within 20 min or $4 within an hour), a model reads what the learner wrote and said in it. Spam —
the account is blocked until the admin unblocks it, the lesson stops; a real lesson goes on, and is
read again only after it spends another settings.spike_recheck_usd. Every reading is kept for the admin."""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import update

from .config import settings
from .db import sessionmaker
from .models import Learner, SpendAlert

log = logging.getLogger(__name__)
_clock = time.monotonic
BLOCKED = "аккаунт приостановлен: слишком большой расход за короткое время. Напишите нам — разберёмся"


@dataclass
class _Session:
    spent: deque = field(default_factory=deque)  # (when, usd) of this session's calls
    total: float = 0.0
    quiet_until: float = 0.0  # session total below which no new reading is made (after an "ok")


_live: dict[str, _Session] = {}  # by the session's sid


def note(S, usd: float) -> None:
    w = _live.setdefault(S.sid, _Session())
    w.spent.append((_clock(), usd))
    w.total += usd
    longest = max(minutes for minutes, _ in settings.spike_windows) * 60
    for sid, other in list(_live.items()):  # sessions long over drop out
        while other.spent and _clock() - other.spent[0][0] > longest:
            other.spent.popleft()
        if not other.spent:
            del _live[sid]


def _spike(w: _Session) -> dict[int, float] | None:
    """Spend in each window, if any window is over its limit."""
    now = _clock()
    sums = {m: sum(u for t, u in w.spent if now - t <= m * 60) for m, _ in settings.spike_windows}
    return sums if any(sums[m] >= cap for m, cap in settings.spike_windows) else None


async def watch(S) -> None:
    """Before every model call of a session. Raises to stop the session when the learner is blocked."""
    w = _live.get(S.sid)
    if w is None or w.total < w.quiet_until or (sums := _spike(w)) is None:
        return
    w.quiet_until = w.total + settings.spike_recheck_usd
    try:
        verdict = await _review(S, sums)
    except Exception as e:  # unsure — the lesson goes on, the admin sees the failed reading
        log.exception("spend review failed")
        verdict = {"spam": False, "reason": f"проверка не удалась: {e}"[:500], "error": True}
    lid = uuid.UUID(S.user)
    async with sessionmaker()() as s:
        s.add(SpendAlert(learner_id=lid, sid=S.sid, lang=S.lang, usd_by_window=sums, session_usd=round(w.total, 4),
                         verdict="error" if verdict.get("error") else "spam" if verdict["spam"] else "ok",
                         reason=verdict["reason"][:500]))
        if verdict["spam"]:
            await s.execute(update(Learner).where(Learner.id == lid).values(
                status="blocked", blocked_reason=f"авто: {verdict['reason']}"[:500],
                blocked_at=datetime.now(timezone.utc)))
        await s.commit()
    if verdict["spam"]:
        raise RuntimeError(BLOCKED)


def _transcript(S, limit: int = 24000) -> str:
    """What the learner and the tutor said in this session, newest kept when it's long."""
    rows = []
    for e in S.log:
        if e.get("type") in ("user", "tutor") and (text := str(e.get("text") or "").strip()):
            rows.append(("УЧЕНИК: " if e["type"] == "user" else "РЕПЕТИТОР: ") + text[:600])
    out, size = [], 0
    for r in reversed(rows):
        if size + len(r) > limit:
            break
        out.append(r)
        size += len(r)
    return "\n".join(reversed(out))


async def _review(S, sums: dict[int, float]) -> dict:
    from openai import AsyncOpenAI

    import lp.openai_tutor as ot
    model = settings.spike_review_model
    learner_msgs = [e for e in S.log if e.get("type") == "user"]
    facts = (f"Режим: {S.mode}, язык: {S.lang}. Расход этой сессии: "
             + ", ".join(f"${usd:.2f} за последние {m} мин" for m, usd in sums.items())
             + f". Обычный урок на 60–90 минут стоит $1–2 целиком. Сообщений ученика: {len(learner_msgs)}, "
             f"символов в них: {sum(len(str(e.get('text') or '')) for e in learner_msgs)}.")
    r = await AsyncOpenAI(api_key=ot._key()).responses.create(
        model=model, reasoning={"effort": "low"},
        instructions="Ты проверяешь урок языкового репетитора, где расход на модель вырос слишком быстро. "
                     "Реши, тратит ли ученик токены намеренно: шлёт мусор, повторы, огромные тексты не по уроку, "
                     "просит репетитора писать длинное ради длины, пытается использовать его как бесплатный "
                     "чат-бот для посторонних задач, заставляет зацикливаться. Медленный, слабый, путающийся или "
                     "много говорящий по делу ученик — не спам. Сомневаешься — не спам: блокировка бьёт по "
                     "живому человеку. reason — одно-два предложения по-русски для администратора.",
        input=f"{facts}\n\nРасшифровка (последнее):\n{_transcript(S)}",
        text={"format": {"type": "json_schema", "name": "verdict", "strict": True, "schema": {
            "type": "object", "additionalProperties": False, "required": ["spam", "reason"],
            "properties": {"spam": {"type": "boolean"}, "reason": {"type": "string"}}}}})
    price_in, price_cached, price_out = ot.PRICES[model]
    u = r.usage
    cached = (u.input_tokens_details.cached_tokens or 0) if u.input_tokens_details else 0
    await ot.book(S, ((u.input_tokens - cached) * price_in + cached * price_cached + u.output_tokens * price_out) / 1e6,
                  "checks")
    return json.loads(r.output_text)
