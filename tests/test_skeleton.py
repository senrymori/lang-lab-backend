from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import update

from app import codes
from app.config import settings
from app.db import sessionmaker
from app.main import app
from app.models import AccessCode


async def test_health():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_invite_is_spent_by_one_registration():
    async with sessionmaker()() as s:
        invite = await codes.create(s, note="Аня", days=90)
    async with sessionmaker()() as s:
        a = await codes.register(s, email="Anya@Example.com", name="Аня", code=invite.code.lower().replace("-", " "))
    assert a.email == "anya@example.com" and a.access_until > datetime.now(timezone.utc) + timedelta(days=89)
    async with sessionmaker()() as s:
        with pytest.raises(codes.CodeError, match="уже использован"):
            await codes.register(s, email="other@example.com", name="Кто-то", code=invite.code)


async def test_expired_and_unknown_codes_fail():
    async with sessionmaker()() as s:
        old = await codes.create(s, note="забытый", days=30, expires_in_days=1)
        await s.execute(update(AccessCode).where(AccessCode.code == old.code)
                        .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await s.commit()
    for code in (old.code, "LAB-NOPE-NOPE"):
        async with sessionmaker()() as s:
            with pytest.raises(codes.CodeError):
                await codes.register(s, email=f"{code}@example.com", name="x", code=code)


async def test_registration_closes_at_the_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_learners", 1)  # one learner already registered above
    async with sessionmaker()() as s:
        promo = await codes.create(s, note="кампания", days=14, kind="promo", max_uses=100)
    async with sessionmaker()() as s:
        with pytest.raises(codes.CodeError, match="закрыт"):
            await codes.register(s, email="late@example.com", name="Поздний", code=promo.code)
