"""The admin's API (/admin/api): learners, access codes, access requests, spend, live lessons. One admin, one key:
`Authorization: Bearer <key>` with the key from LANGLAB_ADMIN_KEY or ~/.config/lp/admin.key; without a key
the admin is off. Cloudflare Access in front of admin.langlab.uk is a second lock, not the only one: the same
app answers on api.langlab.uk too."""
from __future__ import annotations

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
from .models import AccessCode, AccessRequest, AuthSession, Learner, Spend, SpendAlert


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
    from lp import server
    return {"learners": {k: n for k, n in learners}, "max_learners": settings.max_learners,
            "spent_today_usd": float(today), "spent_month_usd": float(this_month), "live_lessons": len(server.sessions),
            "requests": asking}


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
    live = {k.split("/")[0]: s_.mode for k, s_ in server.sessions.items()}
    return [{"id": str(l.id), "email": l.email, "name": l.name, "status": l.status, "code": l.code,
             "lessons_total": l.lessons_total, "lessons_used": l.lessons_used, "access_until": l.access_until, "created_at": l.created_at, "last_seen_at": seen_at,
             "spent_today_usd": float(today or 0), "spent_30d_usd": float(d30 or 0), "spent_total_usd": float(total or 0),
             "blocked_reason": l.blocked_reason, "blocked_at": l.blocked_at, "live": live.get(str(l.id))}
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
async def spend(days: int = 30, s: AsyncSession = Depends(get_db)):
    """Per day, all learners together: dollars and model calls."""
    since = _today() - timedelta(days=max(1, min(days, 365)) - 1)
    rows = (await s.execute(select(Spend.day, func.sum(Spend.usd), func.sum(Spend.calls))
                            .where(Spend.day >= since).group_by(Spend.day).order_by(Spend.day))).all()
    return [{"day": d, "usd": float(u), "calls": int(n)} for d, u, n in rows]


@router.get("/alerts")
async def alerts(limit: int = 50, s: AsyncSession = Depends(get_db)):
    """Sessions that spent too fast, newest first, with what the model reading them decided (app/abuse.py)."""
    rows = (await s.execute(select(SpendAlert, Learner.name, Learner.email).join(Learner)
                            .order_by(SpendAlert.created_at.desc()).limit(max(1, min(limit, 500))))).all()
    return [{"id": a.id, "learner_id": str(a.learner_id), "name": name, "email": email, "sid": a.sid, "lang": a.lang,
             "usd_by_window": a.usd_by_window, "session_usd": float(a.session_usd), "verdict": a.verdict,
             "reason": a.reason, "created_at": a.created_at} for a, name, email in rows]
