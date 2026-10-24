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
