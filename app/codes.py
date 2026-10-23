"""Access codes: made for a person (invite) or a campaign (promo), spent at registration.

  uv run python -m app.codes new --note "Аня, коллега" --days 90 [--expires 14]
  uv run python -m app.codes list
"""
from __future__ import annotations

import argparse
import asyncio
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import AccessCode, Learner

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: codes get read aloud and retyped


class CodeError(ValueError):
    """Shown to the person registering, so in plain Russian."""


def new_code() -> str:
    s = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"LAB-{s[:4]}-{s[4:]}"


def normalize(code: str) -> str:
    """Whatever was typed (spaces, lower case, no dashes) → LAB-XXXX-XXXX."""
    s = re.sub(r"[^A-Z0-9]", "", code.upper())
    s = s[3:] if s.startswith("LAB") else s
    return f"LAB-{s[:4]}-{s[4:]}" if len(s) == 8 else code.strip().upper()


async def create(s: AsyncSession, *, note: str, days: int, kind: str = "invite", max_uses: int = 1,
                 expires_in_days: int | None = None) -> AccessCode:
    c = AccessCode(code=new_code(), kind=kind, days=days, max_uses=1 if kind == "invite" else max_uses,
                   note=note, expires_at=(datetime.now(timezone.utc) + timedelta(days=expires_in_days))
                   if expires_in_days else None)
    s.add(c)
    await s.commit()
    return c


async def register(s: AsyncSession, *, email: str, name: str, code: str) -> Learner:
    """A new learner with the access their code gives. One transaction: the cap is checked under a lock
    and the code is spent with a conditional UPDATE, so two people can't both use the last place or the
    same invite."""
    email = email.strip().lower()
    async with s.begin():
        await s.execute(text("select pg_advisory_xact_lock(4242)"))  # registrations one at a time
        if (await s.execute(select(Learner).where(Learner.email == email))).scalar_one_or_none():
            raise CodeError("Этот адрес уже зарегистрирован — войдите по ссылке из письма")
        if (await s.execute(select(func.count()).select_from(Learner))).scalar_one() >= settings.max_learners:
            raise CodeError("Набор в бету закрыт: все места заняты")
        now = datetime.now(timezone.utc)
        days = (await s.execute(
            update(AccessCode)
            .where(AccessCode.code == normalize(code), AccessCode.active.is_(True),
                   AccessCode.used < AccessCode.max_uses,
                   (AccessCode.expires_at.is_(None)) | (AccessCode.expires_at > now))
            .values(used=AccessCode.used + 1)
            .returning(AccessCode.days))).scalar_one_or_none()
        if days is None:
            raise CodeError("Код не подходит: неверный, уже использован или истёк")
        learner = Learner(email=email, name=name.strip()[:80], code=normalize(code),
                          access_until=now + timedelta(days=days))
        s.add(learner)
    return learner


async def _cli(a) -> None:
    from .db import sessionmaker, stop_dev_db
    try:
        async with sessionmaker()() as s:
            if a.cmd == "new":
                c = await create(s, note=a.note, days=a.days, kind=a.kind, max_uses=a.uses,
                                 expires_in_days=a.expires)
                print(f"{c.code}  ({c.kind}, {c.days} дн. доступа"
                      + (f", сгорит {c.expires_at:%d.%m.%Y}" if c.expires_at else "") + f") — {c.note}")
            else:
                for c in (await s.execute(select(AccessCode).order_by(AccessCode.created_at))).scalars():
                    state = "использован" if c.used >= c.max_uses else ("выключен" if not c.active else "свободен")
                    print(f"{c.code}  {c.kind:6s} {state:11s} {c.used}/{c.max_uses}  {c.days} дн.  {c.note}")
    finally:
        stop_dev_db()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m app.codes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="make a code")
    n.add_argument("--note", required=True, help="who it is for")
    n.add_argument("--days", type=int, default=90, help="access it gives")
    n.add_argument("--kind", choices=["invite", "promo"], default="invite")
    n.add_argument("--uses", type=int, default=1, help="promo only")
    n.add_argument("--expires", type=int, help="unused code burns out after this many days")
    sub.add_parser("list")
    asyncio.run(_cli(ap.parse_args()))
