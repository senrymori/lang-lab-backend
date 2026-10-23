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
        r = await c.post("/admin/api/codes", json={"note": "Аня", "days": 30}, headers=admin)
        assert r.status_code == 200 and r.json()["code"].startswith("LAB-")
        code = r.json()["code"]
        assert any(x["code"] == code for x in (await c.get("/admin/api/codes", headers=admin)).json())
        assert (await c.patch(f"/admin/api/codes/{code}", json={"active": False}, headers=admin)).json()["active"] is False

    token, l = await learner("Боря")
    async with client() as c:
        rows = (await c.get("/admin/api/learners", headers=admin)).json()
        me = next(x for x in rows if x["id"] == str(l.id))
        assert me["status"] == "active" and me["spent_today_usd"] == 0
        r = await c.patch(f"/admin/api/learners/{l.id}", json={"extend_days": 10, "status": "blocked"}, headers=admin)
        assert r.json()["status"] == "blocked"
        assert (await c.get("/admin/api/overview", headers=admin)).json()["learners"]["blocked"] >= 1
    async with sessionmaker()() as s:
        assert all(x.revoked_at for x in (await s.scalars(select(AuthSession).where(AuthSession.learner_id == l.id))))
    async with client(token) as c:
        assert (await c.get("/me")).status_code in (401, 403)
