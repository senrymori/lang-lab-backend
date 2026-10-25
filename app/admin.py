"""The admin's API (/admin/api): learners, access codes, access requests, spend, live lessons. One admin, one key:
`Authorization: Bearer <key>` with the key from LANGLAB_ADMIN_KEY or ~/.config/lp/admin.key; without a key
the admin is off. Cloudflare Access in front of admin.langlab.uk is a second lock, not the only one: the same
app answers on api.langlab.uk too."""
from __future__ import annotations

import asyncio
import contextvars
import hmac
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import auth, codes, mail
from .config import settings
from .db import get_db
from .models import AccessCode, AccessRequest, AuthSession, Learner, Spend, SpendAlert, Usage


def _key() -> str:
    f = settings.admin_key_file
    return settings.admin_key or (f.read_text().strip() if f.is_file() else "")


def require_admin(request: Request) -> None:
    key = _key()
    if not key:
        raise HTTPException(503, "admin is off: no key")
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(got.encode(), key.encode()):
        raise HTTPException(401)


router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return _now().date()


@router.get("/overview")
async def overview(s: AsyncSession = Depends(get_db)):
    month = _today().replace(day=1)
    learners = (await s.execute(select(Learner.status, func.count()).group_by(Learner.status))).all()
    total = lambda since: select(func.coalesce(func.sum(Spend.usd), 0)).where(Spend.day >= since)  # noqa: E731
    today, this_month = (await s.scalar(total(_today()))), (await s.scalar(total(month)))
    asking = await s.scalar(select(func.count()).select_from(AccessRequest))
    by_service = (await s.execute(select(Spend.service, func.sum(Spend.usd)).where(Spend.day >= month)
                                  .group_by(Spend.service))).all()
    used = lambda service, since: select(func.coalesce(func.sum(Usage.amount), 0)).where(  # noqa: E731
        Usage.service == service, Usage.day >= since)
    from lp import server
    return {"learners": {k: n for k, n in learners}, "max_learners": settings.max_learners,
            "spent_today_usd": float(today), "spent_month_usd": float(this_month),
            # sessions the server holds, over ones too (until a restart): only those going count, live or paused
            "live_lessons": sum(1 for o in server.sessions.values() if (p := o.presence()) and p["state"] == "live"),
            "paused_lessons": sum(1 for o in server.sessions.values() if (p := o.presence()) and p["state"] == "paused"),
            "requests": asking, "services_month": {k: float(v) for k, v in by_service},
            "allowances": {"azure_chars_month": int(await s.scalar(used("azure_chars", month))),
                           "azure_chars_limit": settings.azure_free_chars_month,
                           "letters_today": int(await s.scalar(used("letters", _today()))),
                           "letters_day_limit": settings.letters_day_limit,
                           "letters_month": int(await s.scalar(used("letters", month))),
                           "letters_month_limit": settings.letters_month_limit},
            "fixed_costs": settings.fixed_costs}


@router.get("/learners")
async def learners(s: AsyncSession = Depends(get_db)):
    since = _today() - timedelta(days=29)
    seen = select(AuthSession.learner_id, func.max(AuthSession.last_seen_at).label("seen")) \
        .group_by(AuthSession.learner_id).subquery()
    spend = select(Spend.learner_id, func.sum(Spend.usd).filter(Spend.day == _today()).label("today"),
                   func.sum(Spend.usd).filter(Spend.day >= since).label("d30"), func.sum(Spend.usd).label("total")) \
        .group_by(Spend.learner_id).subquery()
    rows = (await s.execute(
        select(Learner, seen.c.seen, spend.c.today, spend.c.d30, spend.c.total)
        .outerjoin(seen, seen.c.learner_id == Learner.id).outerjoin(spend, spend.c.learner_id == Learner.id)
        .order_by(Learner.created_at))).all()
    from lp import server
    # a lesson going (live or paused) — not one over or wrapping up without the learner
    live = {k.split("/")[0]: (s_.mode, p) for k, s_ in server.sessions.items() if (p := s_.presence())}
    return [{"id": str(l.id), "email": l.email, "name": l.name, "status": l.status, "code": l.code,
             "lessons_total": l.lessons_total, "lessons_used": l.lessons_used, "access_until": l.access_until, "created_at": l.created_at, "last_seen_at": seen_at,
             "spent_today_usd": float(today or 0), "spent_30d_usd": float(d30 or 0), "spent_total_usd": float(total or 0),
             "blocked_reason": l.blocked_reason, "blocked_at": l.blocked_at,
             "live": live[str(l.id)][0] if str(l.id) in live else None,
             "presence": live[str(l.id)][1] if str(l.id) in live else None}
            for l, seen_at, today, d30, total in rows]


class LearnerChange(BaseModel):
    status: Literal["active", "blocked"] | None = None
    add_lessons: int | None = Field(None, ge=-1000, le=1000)  # negative takes back (never below those used)
    name: str | None = Field(None, max_length=80)


@router.patch("/learners/{lid}")
async def change_learner(lid: uuid.UUID, body: LearnerChange, s: AsyncSession = Depends(get_db)):
    l = await s.get(Learner, lid)
    if l is None:
        raise HTTPException(404)
    if body.add_lessons:
        l.lessons_total = max(l.lessons_used, l.lessons_total + body.add_lessons)
    if body.name is not None:
        l.name = body.name.strip()
    if body.status:
        l.status = body.status
        l.blocked_reason, l.blocked_at = ("вручную", _now()) if body.status == "blocked" else ("", None)
        if body.status == "blocked":  # a lesson going now stops; the sessions stay, refused with 403 (the app's ban)
            from lp import server
            for k, live in list(server.sessions.items()):
                if k.startswith(f"{lid}/") and live.task:
                    live.task.cancel()
    await s.commit()
    return {"id": str(l.id), "status": l.status, "lessons_total": l.lessons_total, "lessons_used": l.lessons_used,
            "name": l.name}


@router.get("/codes")
async def list_codes(s: AsyncSession = Depends(get_db)):
    rows = (await s.execute(select(AccessCode).order_by(AccessCode.created_at.desc()))).scalars()
    return [_code_json(c) for c in rows]


def _code_json(c: AccessCode) -> dict:
    return {"code": c.code, "kind": c.kind, "name": c.name, "email": c.email, "lessons": c.lessons, "days": c.days,
            "used": c.used, "max_uses": c.max_uses, "note": c.note, "expires_at": c.expires_at, "active": c.active,
            "created_at": c.created_at, "sent": c.link is not None}


class NewCode(BaseModel):
    note: str = Field("", max_length=200)
    name: str = Field("", max_length=80)  # how the tutor addresses them: nobody is asked at sign-up
    email: str | None = Field(None, max_length=320)  # an invite for this address only, mailed with a link
    lessons: int = Field(12, ge=1, le=1000)
    days: int | None = Field(None, ge=1, le=3650)  # a time limit on the lessons, if any
    kind: Literal["invite", "promo"] = "invite"
    max_uses: int = Field(1, ge=1, le=100000)
    expires_in_days: int | None = Field(None, ge=1, le=365)
    request: int | None = None  # made from an access request: that request is done with


@router.post("/codes")
async def new_code(body: NewCode, s: AsyncSession = Depends(get_db)):
    email = (body.email or "").strip().lower() or None
    if email:
        if body.kind != "invite":
            raise HTTPException(400, "Письмом отправляется только личное приглашение")
        if not auth.EMAIL.match(email):
            raise HTTPException(400, "Проверьте адрес почты")
        if (await s.execute(select(Learner.id).where(Learner.email == email))).first():
            raise HTTPException(409, "Этот адрес уже зарегистрирован")
    if body.kind == "invite" and not body.name.strip():
        raise HTTPException(400, "Как репетитору к нему обращаться? Впишите имя")
    c = await codes.create(s, note=body.note, name=body.name, email=email, lessons=body.lessons, days=body.days,
                           kind=body.kind, max_uses=body.max_uses, expires_in_days=body.expires_in_days)
    if body.request is not None:
        await s.execute(delete(AccessRequest).where(AccessRequest.id == body.request))
        await s.commit()
    if email:
        await _send_invite(s, c)
    return _code_json(c)


async def _send_invite(s: AsyncSession, c: AccessCode) -> None:
    """A fresh link (the old one stops working) and the letter with it."""
    t = auth._token()
    c.link = auth._hash(t)
    await s.commit()
    try:
        await mail.send(c.email, *auth.invite_letter(c.name, c.code, f"{settings.public_url.rstrip('/')}/auth/invite?t={t}"))
    except mail.MailError:
        raise HTTPException(502, "Код создан, но письмо не ушло — отправьте ещё раз")


@router.post("/codes/{code}/send")
async def send_invite(code: str, s: AsyncSession = Depends(get_db)):
    c = await s.get(AccessCode, codes.normalize(code))
    if c is None:
        raise HTTPException(404)
    if not c.email or c.used >= c.max_uses or not c.active:
        raise HTTPException(400, "Отправить можно только неиспользованное приглашение с почтой")
    await _send_invite(s, c)
    return _code_json(c)


class CodeChange(BaseModel):
    active: bool


@router.patch("/codes/{code}")
async def change_code(code: str, body: CodeChange, s: AsyncSession = Depends(get_db)):
    c = await s.get(AccessCode, codes.normalize(code))
    if c is None:
        raise HTTPException(404)
    c.active = body.active
    await s.commit()
    return {"code": c.code, "active": c.active}


@router.get("/requests")
async def list_requests(s: AsyncSession = Depends(get_db)):
    """Who asked in from the landing, oldest first: the first to ask is the first to answer."""
    rows = (await s.execute(select(AccessRequest).order_by(AccessRequest.created_at))).scalars()
    return [{"id": r.id, "name": r.name, "email": r.email, "created_at": r.created_at} for r in rows]


@router.delete("/requests/{rid}")
async def dismiss_request(rid: int, s: AsyncSession = Depends(get_db)):
    """Dismissed: nothing of it is kept."""
    if not (await s.execute(delete(AccessRequest).where(AccessRequest.id == rid))).rowcount:
        raise HTTPException(404)
    await s.commit()
    return {"ok": True}


@router.get("/spend")
async def spend(days: int = 30, learner: uuid.UUID | None = None, service: str | None = None,
                s: AsyncSession = Depends(get_db)):
    """Per day: dollars and calls, split by service — all learners together, or one learner, or one service."""
    since = _today() - timedelta(days=max(1, min(days, 365)) - 1)
    q = select(Spend.day, Spend.service, func.sum(Spend.usd), func.sum(Spend.calls)).where(Spend.day >= since)
    if learner:
        q = q.where(Spend.learner_id == learner)
    if service:
        q = q.where(Spend.service == service)
    days_ = {}
    for d, svc, u, n in (await s.execute(q.group_by(Spend.day, Spend.service).order_by(Spend.day))).all():
        row = days_.setdefault(d, {"day": d, "usd": 0.0, "calls": 0, "by_service": {}})
        row["usd"] += float(u)
        row["calls"] += int(n)
        row["by_service"][svc] = float(u)
    return list(days_.values())


@router.get("/spend/detail")
async def spend_detail(days: int = 30, learner: uuid.UUID | None = None, service: str | None = None,
                       s: AsyncSession = Depends(get_db)):
    """Everything behind the spend for a period, filtered by learner and service: the totals by service and by
    learner, and every learner-day-service row (newest first)."""
    since = _today() - timedelta(days=max(1, min(days, 365)) - 1)
    where = [Spend.day >= since] + ([Spend.learner_id == learner] if learner else []) + \
        ([Spend.service == service] if service else [])
    rows = (await s.execute(select(Spend, Learner.name, Learner.email).join(Learner).where(*where)
                            .order_by(Spend.day.desc(), Spend.usd.desc()).limit(2000))).all()
    by_service, by_learner = {}, {}
    for sp, name, email in rows:
        by_service[sp.service] = by_service.get(sp.service, 0.0) + float(sp.usd)
        who = by_learner.setdefault(str(sp.learner_id), {"learner_id": str(sp.learner_id), "name": name,
                                                          "email": email, "usd": 0.0, "calls": 0})
        who["usd"] += float(sp.usd)
        who["calls"] += sp.calls
    return {"total_usd": sum(by_service.values()), "by_service": by_service,
            "by_learner": sorted(by_learner.values(), key=lambda x: -x["usd"]),
            "rows": [{"day": sp.day, "learner_id": str(sp.learner_id), "name": name, "email": email,
                      "service": sp.service, "usd": float(sp.usd), "calls": sp.calls} for sp, name, email in rows]}


@router.get("/alerts")
async def alerts(limit: int = 50, s: AsyncSession = Depends(get_db)):
    """Sessions that spent too fast, newest first, with what the model reading them decided (app/abuse.py)."""
    rows = (await s.execute(select(SpendAlert, Learner.name, Learner.email).join(Learner)
                            .order_by(SpendAlert.created_at.desc()).limit(max(1, min(limit, 500))))).all()
    return [{"id": a.id, "learner_id": str(a.learner_id), "name": name, "email": email, "sid": a.sid, "lang": a.lang,
             "usd_by_window": a.usd_by_window, "session_usd": float(a.session_usd), "verdict": a.verdict,
             "reason": a.reason, "created_at": a.created_at} for a, name, email in rows]


@router.get("/setups")
async def setups(s: AsyncSession = Depends(get_db)):
    """How learners set their lessons up before they start (lp's `setup` events, lp/setup.py): how they start —
    set up, left to the lemur, as last time — over every lesson; what to work on and what to talk about over the ones
    set up by hand (as-last-time repeats would count an old choice again); how many learners have each topic in their
    pool (lp/topics.py — the lemur links what they say to one topic, so «я бизнесмен» and «для бизнеса» count once);
    and every word of their own, newest first, with the topic the lemur linked it to — to see what the cards miss."""
    import lp.core
    import lp.setup
    import lp.store
    import lp.topics
    learners = (await s.execute(select(Learner))).scalars().all()

    def read() -> dict:
        how = {k: 0 for k in lp.setup.HOWS}
        focus = {k: 0 for k in lp.setup.FOCUS}
        chosen: dict[str, int] = {}
        pools: dict[str, int] = {}
        own = []
        for l in learners:
            lp.core.set_user(str(l.id))
            pool = lp.topics.mine() if (settings.data_dir / str(l.id)).is_dir() else []
            for e in pool:
                pools[e["key"]] = pools.get(e["key"], 0) + 1
            linked = {e["from"]: e["title"] for e in pool if e.get("from")}
            for lang in lp.core.LANGS:
                if not (settings.data_dir / str(l.id) / lang / "events").is_dir():
                    continue
                for e in lp.store.load_events(lang):
                    if e["type"] != "setup":
                        continue
                    how[e["how"]] = how.get(e["how"], 0) + 1
                    if e["how"] != "custom":
                        continue
                    if e.get("focus") in focus:
                        focus[e["focus"]] += 1
                    for t in e.get("topics") or []:
                        chosen[t] = chosen.get(t, 0) + 1
                    for key, what in (("focus_own", "Что подтянуть"), ("topic_own", "Своя тема"), ("topic_detail", "Уточнение темы")):
                        if e.get(key):
                            own.append({"learner_id": str(l.id), "name": l.name, "email": l.email, "lang": lang,
                                        "ts": e["ts"], "what": what, "text": e[key],
                                        "topic": next((t for said, t in linked.items() if e[key] in said or said in e[key]), "")})
        cat = {**lp.topics.catalog(), "any": lp.setup.ANY[0]}
        rows = lambda counts, title: sorted(  # noqa: E731
            [{"key": k, "title": title(k), "n": n} for k, n in counts.items()], key=lambda r: -r["n"])
        return {"total": sum(how.values()), "how": how,
                "focus": rows(focus, lambda k: lp.setup.FOCUS[k][0]),
                "topics": rows({**{k: 0 for k in lp.topics.BASE}, **chosen}, lambda k: cat.get(k, k)),
                "pools": rows(pools, lambda k: cat.get(k, k)),
                "own": sorted(own, key=lambda r: r["ts"], reverse=True)}
    return await asyncio.to_thread(contextvars.copy_context().run, read)


@router.get("/feedback")
async def feedback(s: AsyncSession = Depends(get_db)):
    """What learners said about their lessons and placements (lp's `feedback` events): one row per learner and
    language — their level there, the mean rating, each answer. The rows with the newest answer come first."""
    import lp.core
    import lp.store
    learners = (await s.execute(select(Learner))).scalars().all()

    def read() -> list[dict]:
        out = []
        for l in learners:
            for lang in lp.core.LANGS:
                if not (settings.data_dir / str(l.id) / lang / "events").is_dir():
                    continue  # never started it (lang_dir would make the folders)
                lp.core.set_user(str(l.id))
                items = lp.store.feedback(lang)
                if not items:
                    continue
                rated = [i["rating"] for i in items if i.get("rating")]
                out.append({"learner_id": str(l.id), "name": l.name, "email": l.email, "lang": lang,
                            "level": lp.store.level(lang), "rating": sum(rated) / len(rated) if rated else None,
                            "items": items})
        return sorted(out, key=lambda r: r["items"][0]["ts"], reverse=True)
    # lp's files, read in a thread; the learner each read is for is set in a context of its own
    return await asyncio.to_thread(contextvars.copy_context().run, read)
