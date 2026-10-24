import pytest

from app.config import settings

from test_admin import admin  # noqa: F401  the fixture
from test_hosting import client, learner


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(settings, "intake_key", "site-secret")
    return {"X-Intake-Key": "site-secret"}


async def test_closed_without_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "intake_key", None)
    monkeypatch.setattr(settings, "intake_key_file", tmp_path / "none.key")
    async with client() as c:
        assert (await c.post("/intake/access-request", json={"name": "Аня", "email": "a@x.ru"})).status_code == 503
    monkeypatch.setattr(settings, "intake_key", "site-secret")
    async with client() as c:
        r = await c.post("/intake/access-request", json={"name": "Аня", "email": "a@x.ru"}, headers={"X-Intake-Key": "nope"})
        assert r.status_code == 401


async def test_a_request_waits_on_the_admin_and_an_invite_takes_it(site, admin):  # noqa: F811
    async with client() as c:
        assert (await c.post("/intake/access-request", json={"name": "Аня", "email": "nope"}, headers=site)).status_code == 400
        for name in ("Аня", "Анна  Петрова"):  # asking again only refreshes the name
            r = await c.post("/intake/access-request", json={"name": name, "email": " Anna@Example.com "}, headers=site)
            assert r.json() == {"ok": True}
        await c.post("/intake/access-request", json={"name": "Боря", "email": "borya@example.com"}, headers=site)
        rows = (await c.get("/admin/api/requests", headers=admin)).json()
        mine = [x for x in rows if x["email"] in ("anna@example.com", "borya@example.com")]
        assert [(x["name"], x["email"]) for x in mine] == [("Анна Петрова", "anna@example.com"), ("Боря", "borya@example.com")]
        assert (await c.get("/admin/api/overview", headers=admin)).json()["requests"] >= 2

        anna, borya = mine
        r = await c.post("/admin/api/codes", json={"name": "Анна", "email": anna["email"], "lessons": 12,
                                                  "request": anna["id"]}, headers=admin)
        assert r.status_code == 200 and r.json()["sent"] is True
        assert (await c.delete(f"/admin/api/requests/{borya['id']}", headers=admin)).json() == {"ok": True}
        assert (await c.delete(f"/admin/api/requests/{borya['id']}", headers=admin)).status_code == 404
        left = {x["email"] for x in (await c.get("/admin/api/requests", headers=admin)).json()}
        assert not left & {"anna@example.com", "borya@example.com"}


async def test_a_learner_asking_again_leaves_nothing(site, admin):  # noqa: F811
    _, l = await learner("Вера")
    async with client() as c:
        r = await c.post("/intake/access-request", json={"name": "Вера", "email": l.email}, headers=site)
        assert r.json() == {"ok": True}
        assert l.email not in {x["email"] for x in (await c.get("/admin/api/requests", headers=admin)).json()}
