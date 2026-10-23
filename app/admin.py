"""The admin's API (/admin/api): learners, access codes, spend, live lessons. One admin, one key:
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
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import codes
from .config import settings
from .db import get_db
from .models import AccessCode, AuthSession, Learner, Spend


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
    from lp import server
    return {"learners": {k: n for k, n in learners}, "max_learners": settings.max_learners,
            "spent_today_usd": float(today), "spent_month_usd": float(this_month),
            "daily_budget_usd": settings.daily_budget_usd, "live_lessons": len(server.sessions)}


@router.get("/learners")
async def learners(s: AsyncSession = Depends(get_db)):
    since = _today() - timedelta(days=29)
    seen = select(AuthSession.learner_id, func.max(AuthSession.last_seen_at).label("seen")) \
        .group_by(AuthSession.learner_id).subquery()
    spend = select(Spend.learner_id, func.sum(Spend.usd).filter(Spend.day == _today()).label("today"),
                   func.sum(Spend.usd).label("d30")).where(Spend.day >= since).group_by(Spend.learner_id).subquery()
    rows = (await s.execute(
        select(Learner, seen.c.seen, spend.c.today, spend.c.d30)
        .outerjoin(seen, seen.c.learner_id == Learner.id).outerjoin(spend, spend.c.learner_id == Learner.id)
        .order_by(Learner.created_at))).all()
    from lp import server
    live = {k.split("/")[0]: s_.mode for k, s_ in server.sessions.items()}
    return [{"id": str(l.id), "email": l.email, "name": l.name, "status": l.status, "code": l.code,
             "access_until": l.access_until, "created_at": l.created_at, "last_seen_at": seen_at,
             "spent_today_usd": float(today or 0), "spent_30d_usd": float(d30 or 0), "live": live.get(str(l.id))}
            for l, seen_at, today, d30 in rows]


class LearnerChange(BaseModel):
    status: Literal["active", "blocked"] | None = None
    extend_days: int | None = Field(None, ge=1, le=3650)  # from now or from the current end, whichever is later
    name: str | None = Field(None, max_length=80)


@router.patch("/learners/{lid}")
async def change_learner(lid: uuid.UUID, body: LearnerChange, s: AsyncSession = Depends(get_db)):
    l = await s.get(Learner, lid)
    if l is None:
        raise HTTPException(404)
    if body.extend_days:
        l.access_until = max(l.access_until or _now(), _now()) + timedelta(days=body.extend_days)
    if body.name is not None:
        l.name = body.name.strip()
    if body.status:
        l.status = body.status
        if body.status == "blocked":  # signed out everywhere, and a lesson going now stops
            await s.execute(update(AuthSession).where(AuthSession.learner_id == lid, AuthSession.revoked_at.is_(None))
                            .values(revoked_at=_now()))
            from lp import server
            for k, live in list(server.sessions.items()):
                if k.startswith(f"{lid}/") and live.task:
                    live.task.cancel()
    await s.commit()
    return {"id": str(l.id), "status": l.status, "access_until": l.access_until, "name": l.name}


@router.get("/codes")
async def list_codes(s: AsyncSession = Depends(get_db)):
    rows = (await s.execute(select(AccessCode).order_by(AccessCode.created_at.desc()))).scalars()
    return [{"code": c.code, "kind": c.kind, "days": c.days, "used": c.used, "max_uses": c.max_uses, "note": c.note,
             "expires_at": c.expires_at, "active": c.active, "created_at": c.created_at} for c in rows]


class NewCode(BaseModel):
    note: str = Field(max_length=200)
    days: int = Field(ge=1, le=3650)
    kind: Literal["invite", "promo"] = "invite"
    max_uses: int = Field(1, ge=1, le=100000)
    expires_in_days: int | None = Field(None, ge=1, le=365)


@router.post("/codes")
async def new_code(body: NewCode, s: AsyncSession = Depends(get_db)):
    c = await codes.create(s, note=body.note, days=body.days, kind=body.kind, max_uses=body.max_uses,
                           expires_in_days=body.expires_in_days)
    return {"code": c.code, "kind": c.kind, "days": c.days, "max_uses": c.max_uses, "expires_at": c.expires_at}


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


@router.get("/spend")
async def spend(days: int = 30, s: AsyncSession = Depends(get_db)):
    """Per day, all learners together: dollars and model calls."""
    since = _today() - timedelta(days=max(1, min(days, 365)) - 1)
    rows = (await s.execute(select(Spend.day, func.sum(Spend.usd), func.sum(Spend.calls))
                            .where(Spend.day >= since).group_by(Spend.day).order_by(Spend.day))).all()
    return [{"day": d, "usd": float(u), "calls": int(n)} for d, u, n in rows]
