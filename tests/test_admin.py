import pytest
from sqlalchemy import select

from app.config import settings
from app.db import sessionmaker
from app.models import AuthSession

from test_hosting import client, learner


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_key", "admin-secret")
    return {"Authorization": "Bearer admin-secret"}


async def test_closed_without_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "admin_key", None)
    monkeypatch.setattr(settings, "admin_key_file", tmp_path / "none.key")
    async with client() as c:
        assert (await c.get("/admin/api/overview")).status_code == 503
    monkeypatch.setattr(settings, "admin_key", "admin-secret")
    async with client("a-learner-token") as c:
        assert (await c.get("/admin/api/overview")).status_code == 401


async def test_codes_learners_and_blocking(admin):
    async with client() as c:
        assert (await c.post("/admin/api/codes", json={"note": "Аня", "lessons": 12}, headers=admin)).status_code == 400
        r = await c.post("/admin/api/codes", json={"note": "коллега", "name": "Аня", "lessons": 12}, headers=admin)
        assert r.status_code == 200 and r.json()["code"].startswith("LAB-") and r.json()["name"] == "Аня"
        code = r.json()["code"]
        assert any(x["code"] == code for x in (await c.get("/admin/api/codes", headers=admin)).json())
        assert (await c.patch(f"/admin/api/codes/{code}", json={"active": False}, headers=admin)).json()["active"] is False

    token, l = await learner("Боря")
    async with client() as c:
        rows = (await c.get("/admin/api/learners", headers=admin)).json()
        me = next(x for x in rows if x["id"] == str(l.id))
        assert me["status"] == "active" and me["spent_today_usd"] == 0
        r = await c.patch(f"/admin/api/learners/{l.id}", json={"add_lessons": 3, "status": "blocked"}, headers=admin)
        assert r.json()["status"] == "blocked" and r.json()["lessons_total"] == 15
        assert (await c.get("/admin/api/overview", headers=admin)).json()["learners"]["blocked"] >= 1
    async with client(token) as c:  # the session stays and says «blocked» (the app shows the ban), and nothing else works
        assert (await c.get("/me")).status_code == 403
        assert (await c.post("/auth/ws-ticket")).status_code == 403


async def test_the_admin_only_on_its_host(admin, monkeypatch):
    import httpx
    from app.main import app
    monkeypatch.setattr(settings, "admin_host", "admin.example.com")

    def on(host):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}")
    async with on("api.example.com") as c:
        assert (await c.get("/admin/api/overview", headers=admin)).status_code == 404
        assert (await c.get("/health")).status_code == 200
    async with on("admin.example.com") as c:
        assert (await c.get("/admin/api/overview", headers=admin)).status_code == 200
        r = await c.get("/api/overview")
        assert r.status_code == 307 and r.headers["location"] == "/admin/"


async def test_a_blocked_learner_can_still_sign_out(admin):
    token, l = await learner("Ушедший")
    async with client() as c:
        await c.patch(f"/admin/api/learners/{l.id}", json={"status": "blocked"}, headers=admin)
    async with client(token) as c:
        assert (await c.get("/me")).status_code == 403  # the ban screen
        assert (await c.post("/auth/logout")).status_code == 200  # its «Выйти»
        assert (await c.get("/me")).status_code == 401  # signed out: the sign-in screen


async def test_spend_by_service_the_detail_and_the_free_allowances(admin, monkeypatch):
    from types import SimpleNamespace

    from app import abuse, hosting

    async def a_lesson(S, sums):
        return {"spam": False, "reason": "урок"}
    monkeypatch.setattr(abuse, "_review", a_lesson)
    _, a = await learner("Аня")
    _, b = await learner("Боб")
    A, B = SimpleNamespace(user=str(a.id), sid="a", lang="it"), SimpleNamespace(user=str(b.id), sid="b", lang="es")
    await hosting.book(A, 0.5)                # the tutor
    await hosting.book(A, 0.25)
    await hosting.book(A, 0.1, "speech")
    await hosting.book(B, 0.02, "checks")
    await hosting.count("azure_chars", 1200)
    await hosting.count("letters", 2)
    async with client() as c:
        o = (await c.get("/admin/api/overview", headers=admin)).json()
        assert o["services_month"]["tutor"] >= 0.75 and o["services_month"]["speech"] >= 0.1
        assert o["allowances"]["azure_chars_month"] >= 1200 and o["allowances"]["letters_today"] >= 2
        assert o["allowances"]["azure_chars_limit"] == settings.azure_free_chars_month and o["fixed_costs"]
        day = (await c.get("/admin/api/spend", params={"learner": str(a.id)}, headers=admin)).json()[-1]
        assert day["by_service"] == {"tutor": 0.75, "speech": 0.1} and day["calls"] == 3
        d = (await c.get("/admin/api/spend/detail", params={"learner": str(a.id)}, headers=admin)).json()
        assert d["by_service"] == {"tutor": 0.75, "speech": 0.1} and [x["name"] for x in d["by_learner"]] == ["Аня"]
        d = (await c.get("/admin/api/spend/detail", params={"service": "checks"}, headers=admin)).json()
        assert {r["name"] for r in d["rows"]} == {"Боб"} and d["total_usd"] == pytest.approx(0.02, abs=1e-6)
        rows = (await c.get("/admin/api/learners", headers=admin)).json()
        assert next(x for x in rows if x["id"] == str(a.id))["spent_today_usd"] == pytest.approx(0.85)  # all services


async def test_feedback_by_learner_and_language(admin):
    # «Отзывы»: per learner and language — the level, the mean rating, each answer (a placement's told apart)
    import contextvars

    import lp.core
    import lp.store
    _, l = await learner("Вера")

    def write():
        lp.core.set_user(str(l.id))
        lp.store.append("es", [{"type": "skill", "skill": "grammar", "level": "B1", "conf": 0.8, "src": "placement:final"},
                               {"type": "feedback", "rating": 3, "comment": "много вопросов"}], sid="p1")
        lp.store.append("es", [{"type": "feedback", "mode": "learn", "rating": 5, "disliked": ["песня"]}], sid="l1")
    contextvars.copy_context().run(write)
    async with client() as c:
        rows = (await c.get("/admin/api/feedback", headers=admin)).json()
    mine = [r for r in rows if r["learner_id"] == str(l.id)]
    assert len(mine) == 1 and mine[0]["lang"] == "es" and mine[0]["level"].startswith("B1") and mine[0]["rating"] == 4
    modes = {i["sid"]: i["mode"] for i in mine[0]["items"]}
    assert modes == {"p1": "placement", "l1": "learn"}


async def test_a_lesson_over_is_not_on_a_lesson_and_one_stepped_away_is_paused(admin):
    """The server keeps sessions until a restart: one over doesn't count as «on a lesson»; one going without the
    learner at it (the app closed) is «paused»."""
    from types import SimpleNamespace

    from lp import server
    (_, over), (_, away) = await learner("Гриша"), await learner("Алмаз")
    sessions = {f"{over.id}/en": SimpleNamespace(mode="placement", presence=lambda: None),
                f"{away.id}/en": SimpleNamespace(mode="placement", presence=lambda: {"state": "paused", "why": "app"})}
    server.sessions.update(sessions)
    try:
        async with client() as c:
            o = (await c.get("/admin/api/overview", headers=admin)).json()
            assert o["live_lessons"] == 0 and o["paused_lessons"] == 1
            rows = {x["id"]: x for x in (await c.get("/admin/api/learners", headers=admin)).json()}
        assert rows[str(over.id)]["live"] is None and rows[str(over.id)]["presence"] is None
        assert rows[str(away.id)]["live"] == "placement" and rows[str(away.id)]["presence"]["why"] == "app"
    finally:
        for k in sessions:
            server.sessions.pop(k, None)


async def test_lesson_setups_counted_and_their_own_words(admin):
    """What learners choose before a lesson: how they start over all lessons, the cards over the ones set up by hand,
    and their own words — to see what the choices are missing."""
    import lp.core
    import lp.store
    _, l = await learner("Тимур")
    lp.core.use_learner(str(l.id), "Тимур")
    lp.store.append("en", [{"type": "setup", "how": "custom", "focus": "grammar", "topic": "games", "topic_detail": "Dota 2"}])
    lp.store.append("en", [{"type": "setup", "how": "custom", "focus_own": "фразовые глаголы для работы", "topic": "any"}])
    lp.store.append("en", [{"type": "setup", "how": "last", "focus": "grammar", "topic": "games"}])
    lp.store.append("en", [{"type": "setup", "how": "lemur"}])
    async with client() as c:
        r = (await c.get("/admin/api/setups", headers=admin)).json()
    assert r["how"] == {"custom": 2, "lemur": 1, "last": 1} and r["total"] == 4
    assert r["focus"][0] == {"key": "grammar", "title": "Грамматика", "n": 1}  # «как в прошлый раз» not counted again
    assert {(o["what"], o["text"]) for o in r["own"]} == {("Конкретно", "Dota 2"), ("Что подтянуть", "фразовые глаголы для работы")}
