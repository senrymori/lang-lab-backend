import io
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi import HTTPException
from sqlalchemy import update

from app import auth, codes, hosting
from app.config import settings
from app.db import sessionmaker
from app.main import app
from app.models import AuthSession, Learner


def client(token: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"Authorization": f"Bearer {token}"} if token else {})


async def learner(name: str, days: int | None = 90, lessons: int = 12) -> tuple[str, Learner]:
    """A registered learner signed in on one device: (token, learner)."""
    async with sessionmaker()() as s:
        code = await codes.create(s, note=name, lessons=lessons, days=days)
    async with sessionmaker()() as s:
        l = await codes.register(s, email=f"{uuid.uuid4().hex[:8]}@example.com", name=name, code=code.code)
    token = auth._token()
    async with sessionmaker()() as s:
        s.add(AuthSession(id=auth._hash(token), learner_id=l.id))
        await s.commit()
    return token, l


@pytest.fixture
def no_tutor(monkeypatch):
    """A session starts but never calls a model."""
    import lp.tutor

    async def idle(self):
        pass
    monkeypatch.setattr(lp.tutor.TutorSession, "run", idle)


async def test_the_page_is_open_the_data_is_not():
    async with client() as c:
        assert (await c.get("/")).status_code == 200
        assert (await c.get("/static/app.js")).status_code == 200
        assert (await c.get("/api/overview")).status_code == 401
        assert (await c.get("/api/overview", headers={"Authorization": "Bearer nonsense"})).status_code == 401


async def test_learners_see_only_their_own():
    (ta, a), (tb, b) = await learner("Аня"), await learner("Боря")
    async with client(ta) as c:
        assert (await c.get("/api/overview")).status_code == 200
        r = await c.put("/api/learner", json={"interests": "футбол"})
        assert r.status_code == 200
        me = (await c.get("/api/learner")).json()
        assert me["name"] == "Аня" and me["values"]["interests"] == "футбол"
        assert (await c.get("/api/users")).json()["users"] == [{"id": str(a.id), "name": "Аня"}]
    async with client(tb) as c:
        me = (await c.get("/api/learner")).json()
        assert me["name"] == "Боря" and me["values"]["interests"] == ""
    assert (settings.data_dir / str(a.id) / "learner.json").exists()
    assert not (settings.data_dir / str(b.id) / "learner.json").exists()


async def test_someone_elses_recording_is_not_served():
    (ta, a), (tb, b) = await learner("Вера"), await learner("Гоша")
    d = settings.recordings_dir / str(a.id) / "en" / "2026-10-23"
    d.mkdir(parents=True)
    (d / "120000.webm").write_bytes(b"x")
    async with client(ta) as c:
        assert (await c.get(f"/rec/{a.id}/en/2026-10-23/120000.webm")).status_code == 200
    async with client(tb) as c:
        assert (await c.get(f"/rec/{a.id}/en/2026-10-23/120000.webm")).status_code == 404


async def test_workshop_and_sync_are_not_for_learners():
    token, _ = await learner("Даша")
    async with client(token) as c:
        for method, path in [("GET", "/api/games"), ("POST", "/api/games/build"), ("POST", "/api/sync"),
                             ("GET", "/api/games/x/page")]:
            assert (await c.request(method, path, json={})).status_code == 404, path


async def test_session_needs_access(no_tutor):
    token, l = await learner("Егор")
    async with client(token) as c:
        assert (await c.post("/api/session/en/start", json={"mode": "../../etc"})).status_code == 400
        r = await c.post("/api/session/en/start", json={"mode": "placement"})
        assert r.status_code == 200 and r.json()["resumed"] is False

    token, l = await learner("Зоя")
    async with sessionmaker()() as s:
        await s.execute(update(Learner).where(Learner.id == l.id)
                        .values(access_until=datetime.now(timezone.utc) - timedelta(days=1)))
        await s.commit()
    async with client(token) as c:
        r = await c.post("/api/session/en/start", json={"mode": "learn"})
        assert r.status_code == 403 and "Доступ закончился" in r.json()["detail"]
        assert (await c.get("/me")).json()["access"] is False


async def test_a_lesson_or_a_placement_is_used_as_it_starts(no_tutor, monkeypatch):
    token, l = await learner("Кира", days=None, lessons=2)
    async with client(token) as c:
        assert (await c.get("/me")).json()["lessons_left"] == 2
        assert (await c.post("/api/session/en/start", json={"mode": "learn"})).status_code == 200
        assert (await c.post("/api/session/es/start", json={"mode": "placement"})).status_code == 200
        r = await c.post("/api/session/kk/start", json={"mode": "placement"})
        assert r.status_code == 403 and "Уроки закончились" in r.json()["detail"]
        me = (await c.get("/me")).json()
        assert me["lessons_left"] == 0 and me["access"] is True and me["access_until"] is None
    monkeypatch.setattr(settings, "admin_key", "k")
    async with client() as c:
        r = await c.patch(f"/admin/api/learners/{l.id}", json={"add_lessons": 2}, headers={"Authorization": "Bearer k"})
        assert r.json()["lessons_total"] == 4
    async with client(token) as c:
        assert (await c.post("/api/session/en/start", json={"mode": "learn"})).status_code == 200


async def test_spend_is_booked_and_never_stops_a_session(monkeypatch):
    """Money alone stops nothing: only a model reading the session as spam does (tests/test_abuse.py)."""
    from types import SimpleNamespace

    from app import abuse

    async def a_lesson(S, sums):
        return {"spam": False, "reason": "урок"}
    monkeypatch.setattr(abuse, "_review", a_lesson)
    _, l = await learner("Илья")
    S = SimpleNamespace(user=str(l.id), sid="booked", lang="en")
    await hosting.check(S)
    await hosting.book(S, 0.4)
    await hosting.book(S, 0.6)
    assert await hosting.spent_today(l.id) == pytest.approx(1.0)
    await hosting.book(S, 100)
    await hosting.check(S)


def wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(16000 * seconds), dtype="float32"), 16000, format="WAV")
    return buf.getvalue()


async def lesson_pass(c, l, lang="en") -> str:
    import lp.server
    assert (await c.post(f"/api/session/{lang}/start", json={"mode": "learn"})).status_code == 200
    return lp.server.sessions[f"{l.id}/{lang}"].pass_


async def test_a_recording_too_long_is_refused(no_tutor, monkeypatch):
    import lp.server
    token, l = await learner("Кира")
    monkeypatch.setattr(lp.server, "AUDIO_MAX_SECONDS", 2)
    async with client(token) as c:
        p = await lesson_pass(c, l)
        r = await c.post("/api/transcribe/en", files={"file": ("a.webm", wav(5), "audio/webm")}, headers={"X-LP-Pass": p})
    assert r.status_code == 413 and "длиннее" in r.json()["detail"]


async def test_speech_needs_the_pass_of_a_lesson_going_on(no_tutor, monkeypatch):
    import lp.server
    token, l = await learner("Без урока")
    (other, _) = await learner("Чужой")
    async with client(token) as c:
        for path, kw in [("/api/transcribe/en", {"files": {"file": ("a.webm", wav(1), "audio/webm")}}),
                         ("/api/hear/en", {"files": {"file": ("a.webm", wav(1), "audio/webm")}}),
                         ("/api/say/en", {"json": {"text": "hello"}})]:
            assert (await c.post(path, **kw)).status_code == 403, path
        p = await lesson_pass(c, l)
        assert (await c.post("/api/say/en", json={"text": "hi"}, headers={"X-LP-Pass": "nope"})).status_code == 403
        assert (await c.post("/api/say/es", json={"text": "hi"}, headers={"X-LP-Pass": p})).status_code == 403
    async with client(other) as c:  # someone else's pass is no use
        assert (await c.post("/api/say/en", json={"text": "hi"}, headers={"X-LP-Pass": p})).status_code == 403


async def test_recognitions_a_minute_are_limited_per_learner(no_tutor, monkeypatch):
    import lp.server
    import lp.stt
    monkeypatch.setattr(lp.server, "HEAR_PER_MINUTE", 2)
    monkeypatch.setattr(lp.stt, "transcribe", lambda *a, **k: {"text": "hi", "cost": 0.001})
    token, l = await learner("Торопыга")
    async with client(token) as c:
        p = await lesson_pass(c, l)
        codes = [(await c.post("/api/transcribe/en", files={"file": ("a.webm", wav(1), "audio/webm")},
                               headers={"X-LP-Pass": p})).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert await hosting.spent_today(l.id) == pytest.approx(0.002)  # recognition is booked on the learner


async def test_one_lesson_at_a_time(monkeypatch):
    import asyncio

    import lp.server
    import lp.tutor

    async def going(self):  # a lesson that hasn't ended
        await asyncio.Event().wait()
    monkeypatch.setattr(lp.tutor.TutorSession, "run", going)
    token, l = await learner("Двоечник")
    async with client(token) as c:
        await lesson_pass(c, l, "en")
        r = await c.post("/api/session/kk/start", json={"mode": "learn"})
        assert r.status_code == 409 and "закончите" in r.json()["detail"]
        assert (await c.post("/api/session/en/start", json={"mode": "learn"})).json()["resumed"] is True
    lp.server.sessions[f"{l.id}/en"].task.cancel()


async def test_websocket_signs_in_by_cookie_or_a_one_time_ticket():
    import lp.core
    token, l = await learner("Лев")
    # the token itself never goes in a URL: logs keep URLs
    scope = {"type": "websocket", "path": "/ws/en", "query_string": f"token={token}".encode(), "headers": []}
    assert await hosting.identify(scope) is False
    async with client(token) as c:
        ticket = (await c.post("/auth/ws-ticket")).json()["ticket"]
    scope = {"type": "websocket", "path": "/ws/en", "query_string": f"ticket={ticket}".encode(), "headers": []}
    assert await hosting.identify(scope) is True and lp.core.user() == str(l.id)
    assert await hosting.identify(scope) is False  # spent
    scope = {"type": "websocket", "path": "/ws/en", "query_string": b"",
             "headers": [(b"cookie", f"{auth.COOKIE}={token}".encode())]}
    assert await hosting.identify(scope) is True
    assert await hosting.identify({"type": "websocket", "path": "/ws/en", "query_string": b"",
                                   "headers": []}) is False
    # an http request never takes the token from the query string: it would end up in logs
    assert await hosting.identify({"type": "http", "path": "/api/overview", "query_string": f"token={token}".encode(),
                                   "headers": []}) is False


async def test_a_practice_game_gets_a_pass_of_its_own(monkeypatch):
    import lp.core
    import lp.server
    _, l = await learner("Игрок")
    lp.core.use_learner(str(l.id), "Игрок")
    p = lp.server._practice_pass("kk")
    assert lp.server._pass("kk", p).mode == "practice"
    with pytest.raises(HTTPException):
        lp.server._pass("en", p)  # for its own language only
    monkeypatch.setattr(lp.server, "PRACTICE_PASS_HOURS", -1)
    stale = lp.server._practice_pass("kk")
    with pytest.raises(HTTPException):
        lp.server._pass("kk", stale)


async def test_profile_fields_have_a_limit_and_the_tutor_s_additions_are_retold(monkeypatch):
    import lp.core
    import lp.openai_tutor
    import lp.tutor
    token, l = await learner("Писатель")
    async with client(token) as c:
        r = await c.put("/api/learner", json={"about": "я" * (lp.core.LEARNER_MAX + 1)})
        assert r.status_code == 400 and "длиннее" in r.json()["detail"]
        assert (await c.put("/api/learner", json={"about": "я" * 900})).status_code == 200
    lp.core.use_learner(str(l.id), "Писатель")
    seen = []

    async def retold(S, label, text, limit):
        seen.append((label, len(text), limit))
        return "коротко"
    monkeypatch.setattr(lp.openai_tutor, "squeeze", retold)
    out = await lp.tutor.save_learner_fields(None, {"about": "я" * 900 + "\nновое " * 30})
    assert out["about"] == "коротко" and seen[0][2] == lp.core.LEARNER_SQUEEZE_TO

    async def failed(S, label, text, limit):
        return None
    monkeypatch.setattr(lp.openai_tutor, "squeeze", failed)
    out = await lp.tutor.save_learner_fields(None, {"interests": "старое\n" * 200 + "новое"})
    assert len(out["interests"]) <= lp.core.LEARNER_MAX and out["interests"].endswith("новое")
    assert not out["interests"].startswith("тарое")  # cut at a line, not mid-word


async def test_three_own_messages_unanswered_and_the_learner_waits_for_the_tutor(no_tutor, monkeypatch):
    import lp.server
    import lp.tutor
    monkeypatch.setattr(lp.tutor, "UNANSWERED_MAX", 3)
    token, l = await learner("Болтушка")
    async with client(token) as c:
        p = await lesson_pass(c, l)
        s = lp.server.sessions[f"{l.id}/en"]
        for n in range(3):
            assert not s.held()
            await s.user_message(f"привет {n}")
        assert s.held()
        r = await c.post("/api/voice/en", files={"file": ("a.webm", wav(1), "audio/webm")}, headers={"X-LP-Pass": p})
        assert r.status_code == 429 and "Дождитесь" in r.json()["detail"]
        await s.emit({"type": "filler", "text": "думаю"}, keep=False)  # not an answer
        assert s.held()
        await s.emit({"type": "tutor", "text": "Отвечаю"})
        assert not s.held()
        await s.emit({"type": "user", "text": "ответ на задание"})  # answers to its tasks aren't counted
        assert s.unanswered == 0
        for n in range(3):
            await s.user_message(f"ещё {n}")
        await s.emit({"type": "status", "state": "idle"}, keep=False)  # its messages still wait in the inbox
        assert s.held()
        while not s.inbox.empty():
            s.inbox.get_nowait()
        await s.emit({"type": "status", "state": "idle"}, keep=False)  # the tutor took them all, showed nothing
        assert not s.held()
