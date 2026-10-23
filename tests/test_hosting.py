import io
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import update

from app import auth, codes, hosting
from app.config import settings
from app.db import sessionmaker
from app.main import app
from app.models import AuthSession, Learner


def client(token: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"Authorization": f"Bearer {token}"} if token else {})


async def learner(name: str, days: int = 90) -> tuple[str, Learner]:
    """A registered learner signed in on one device: (token, learner)."""
    async with sessionmaker()() as s:
        code = await codes.create(s, note=name, days=days)
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


async def test_session_needs_access_and_budget(no_tutor):
    token, l = await learner("Егор")
    async with client(token) as c:
        assert (await c.post("/api/session/en/start", json={"mode": "../../etc"})).status_code == 400
        r = await c.post("/api/session/en/start", json={"mode": "placement"})
        assert r.status_code == 200 and r.json()["resumed"] is False

    token, l = await learner("Жанна")
    from types import SimpleNamespace
    await hosting.book(SimpleNamespace(user=str(l.id)), settings.daily_budget_usd)
    async with client(token) as c:
        r = await c.post("/api/session/en/start", json={"mode": "learn"})
        assert r.status_code == 429 and "лимит" in r.json()["detail"]

    token, l = await learner("Зоя")
    async with sessionmaker()() as s:
        await s.execute(update(Learner).where(Learner.id == l.id)
                        .values(access_until=datetime.now(timezone.utc) - timedelta(days=1)))
        await s.commit()
    async with client(token) as c:
        r = await c.post("/api/session/en/start", json={"mode": "learn"})
        assert r.status_code == 403 and "Доступ закончился" in r.json()["detail"]
        assert (await c.get("/me")).json()["access"] is False


async def test_spend_is_booked_and_stops_a_session_past_the_grace():
    from types import SimpleNamespace
    _, l = await learner("Илья")
    S = SimpleNamespace(user=str(l.id))
    await hosting.check(S)
    await hosting.book(S, 0.4)
    await hosting.book(S, 0.6)
    assert await hosting.spent_today(l.id) == pytest.approx(1.0)
    await hosting.check(S)  # under budget + grace
    await hosting.book(S, settings.daily_budget_usd + settings.budget_grace_usd)
    with pytest.raises(RuntimeError, match="лимит"):
        await hosting.check(S)


async def test_a_recording_too_long_is_refused(monkeypatch):
    import lp.server
    token, _ = await learner("Кира")
    monkeypatch.setattr(lp.server, "AUDIO_MAX_SECONDS", 2)
    buf = io.BytesIO()
    sf.write(buf, np.zeros(16000 * 5, dtype="float32"), 16000, format="WAV")
    async with client(token) as c:
        r = await c.post("/api/transcribe/en", files={"file": ("a.webm", buf.getvalue(), "audio/webm")})
    assert r.status_code == 413 and "длиннее" in r.json()["detail"]


async def test_websocket_signs_in_by_cookie_or_token():
    import lp.core
    token, l = await learner("Лев")
    scope = {"type": "websocket", "path": "/ws/en", "query_string": f"token={token}".encode(), "headers": []}
    assert await hosting.identify(scope) is True and lp.core.user() == str(l.id)
    scope = {"type": "websocket", "path": "/ws/en", "query_string": b"",
             "headers": [(b"cookie", f"{auth.COOKIE}={token}".encode())]}
    assert await hosting.identify(scope) is True
    assert await hosting.identify({"type": "websocket", "path": "/ws/en", "query_string": b"",
                                   "headers": []}) is False
    # an http request never takes the token from the query string: it would end up in logs
    assert await hosting.identify({"type": "http", "path": "/api/overview", "query_string": f"token={token}".encode(),
                                   "headers": []}) is False
