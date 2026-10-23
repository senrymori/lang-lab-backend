"""Who may use the app and what it costs: learners, access codes, spend. The learning itself (events,
notes, practice) stays in the `lp` learner model, in the learner's data folder."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Learner(Base):
    __tablename__ = "learners"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    # access is counted in lessons: a code gives some, the admin adds more; a lesson is used when it starts
    lessons_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lessons_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # optionally also a date (none — no end); kept for paid periods later
    access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | blocked
    blocked_reason: Mapped[str] = mapped_column(String(500), default="", server_default="")  # "авто: …" from app/abuse.py
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    code: Mapped[str | None] = mapped_column(ForeignKey("access_codes.code"))  # the code they joined with
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessCode(Base):
    """A way in. `invite` — personal, made for one person and spent by one registration (the closed
    beta); `promo` — for marketing, many uses. Spent atomically in `codes.register`."""
    __tablename__ = "access_codes"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), default="invite")  # invite | promo
    lessons: Mapped[int] = mapped_column(Integer, default=12, server_default="12")  # lessons it gives
    days: Mapped[int | None] = mapped_column(Integer)  # and a time limit on them, if any
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(200), default="")  # who it is for, where it was posted
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # unused codes burn out
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Spend(Base):
    """Model spend per learner and day, for the admin."""
    __tablename__ = "spend"

    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("learners.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)


class SpendAlert(Base):
    """One session spending too fast, and what the model reading it decided (app/abuse.py)."""
    __tablename__ = "spend_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("learners.id"), index=True)
    sid: Mapped[str] = mapped_column(String(80))
    lang: Mapped[str] = mapped_column(String(8))
    usd_by_window: Mapped[dict] = mapped_column(JSON)  # {"20": 2.1, "60": 2.4}: minutes → USD
    session_usd: Mapped[float] = mapped_column(Numeric(10, 4))
    verdict: Mapped[str] = mapped_column(String(8))  # spam | ok | error
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class LoginRequest(Base):
    """One emailed sign-in (or registration). The app holds the request token, the letter holds the link
    and the code; only their hashes are kept. `purpose` none — an address we don't know: no letter goes
    out, but the app sees the same answer, so nobody learns who is registered."""
    __tablename__ = "login_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the app's request token
    link: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of the link token
    code: Mapped[str] = mapped_column(String(64))  # sha256 of id + the six digits
    email: Mapped[str] = mapped_column(String(320), index=True)
    purpose: Mapped[str] = mapped_column(String(10))  # login | register | none
    name: Mapped[str] = mapped_column(String(80), default="")  # register: how to address them
    access_code: Mapped[str | None] = mapped_column(String(40))  # register: spent only when confirmed
    ip: Mapped[str] = mapped_column(String(64), default="", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # codes typed into the app
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # the link's button pressed
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # a session was issued


class AuthSession(Base):
    """A signed-in device: the app sends `Authorization: Bearer <token>`; only the token's hash is kept."""
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the token
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("learners.id"), index=True)
    device: Mapped[str] = mapped_column(String(200), default="")  # User-Agent, for the admin and the learner
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
