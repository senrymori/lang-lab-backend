import itertools
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from app import codes, mail
from app.db import sessionmaker
from app.main import app
from app.models import AccessCode, Learner, LoginRequest

_ips = (f"10.0.0.{i}" for i in itertools.count(1))


def client(ip: str | None = None) -> httpx.AsyncClient:
    """Every test comes from an address of its own, so the per-address limit doesn't leak between tests."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip or next(_ips), 5000)),
                             base_url="http://t")


def letter_to(email: str) -> tuple[str, str]:
    """The six digits and the link token from the last letter to this address."""
    m = [x for x in mail.outbox if x["to"] == [email]][-1]
    return re.search(r"код: (\d{6})", m["text"])[1], re.search(r"/auth/link\?t=(\S+)", m["text"])[1]


async def invite(note="тест", name="Ученик") -> str:
    async with sessionmaker()() as s:
        return (await codes.create(s, note=note, name=name, days=90)).code


async def test_register_by_code_and_sign_in():
    code = await invite(name="Маша")
    async with client() as c:
        r = await c.post("/auth/start", json={"email": "Masha@Example.com", "code": code.lower(), "name": "Маша"})
        assert r.status_code == 200
        req = r.json()["request"]
        async with sessionmaker()() as s:  # nothing spent until the address is confirmed
            assert (await s.get(AccessCode, code)).used == 0
        assert (await c.post("/auth/poll", json={"request": req})).status_code == 202
        digits, _ = letter_to("masha@example.com")
        wrong = "000000" if digits != "000000" else "111111"
        assert (await c.post("/auth/code", json={"request": req, "code": wrong})).status_code == 400
        r = await c.post("/auth/code", json={"request": req, "code": f"{digits[:3]} {digits[3:]}"})
        assert r.status_code == 200, r.text
        token, me = r.json()["token"], r.json()["learner"]
        assert me["email"] == "masha@example.com" and me["name"] == "Маша" and me["access"] is True
        r = await c.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.json()["email"] == "masha@example.com"
        assert (await c.post("/auth/code", json={"request": req, "code": digits})).status_code == 410
    async with sessionmaker()() as s:
        assert (await s.get(AccessCode, code)).used == 1


async def test_link_needs_the_button_then_the_app_signs_in():
    code = await invite(name="Петя")
    async with client() as c:
        req = (await c.post("/auth/start", json={"email": "petya@example.com", "code": code, "name": "Петя"})).json()["request"]
        await c.post("/auth/code", json={"request": req, "code": letter_to("petya@example.com")[0]})
        # now a known address: a plain sign-in by the link
        req = (await c.post("/auth/start", json={"email": "petya@example.com"})).json()["request"]
        _, link = letter_to("petya@example.com")
        page = await c.get(f"/auth/link?t={link}")
        assert page.status_code == 200 and "<form" in page.text
        assert (await c.post("/auth/poll", json={"request": req})).status_code == 202  # opening it did nothing
        assert "Готово" in (await c.post("/auth/link", data={"t": link})).text
        assert "уже подтверждён" in (await c.post("/auth/link", data={"t": link})).text
        r = await c.post("/auth/poll", json={"request": req})
        assert r.status_code == 200 and r.json()["learner"]["name"] == "Петя"
        assert (await c.post("/auth/poll", json={"request": req})).status_code == 410  # one session per letter
        token = r.json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        assert (await c.post("/auth/logout", headers=h)).status_code == 200
        assert (await c.get("/me", headers=h)).status_code == 401


async def test_unknown_address_gets_the_same_answer_and_no_letter():
    n = len(mail.outbox)
    async with client() as c:
        r = await c.post("/auth/start", json={"email": "nobody@example.com"})
        assert r.status_code == 200 and r.json()["request"]
        assert (await c.post("/auth/poll", json={"request": r.json()["request"]})).status_code == 202
        assert (await c.post("/auth/code", json={"request": r.json()["request"], "code": "123456"})).status_code == 400
    assert len(mail.outbox) == n


async def test_a_bad_code_sends_nothing():
    n = len(mail.outbox)
    async with client() as c:
        r = await c.post("/auth/start", json={"email": "x@example.com", "code": "LAB-NOPE-NOPE"})
        assert r.status_code == 400 and "Код не подходит" in r.json()["detail"]
        assert (await c.post("/auth/start", json={"email": "not-an-email"})).status_code == 400
    assert len(mail.outbox) == n


async def test_a_mistyped_address_keeps_the_invite():
    code = await invite()
    async with client() as c:
        await c.post("/auth/start", json={"email": "typo@exmaple.com", "code": code, "name": "Оля"})
        req = (await c.post("/auth/start", json={"email": "olya@example.com", "code": code, "name": "Оля"})).json()["request"]
        r = await c.post("/auth/code", json={"request": req, "code": letter_to("olya@example.com")[0]})
        assert r.status_code == 200
    async with sessionmaker()() as s:
        assert (await s.execute(select(Learner).where(Learner.email == "typo@exmaple.com"))).first() is None


async def test_limits_letters_and_guesses():
    code = await invite()
    async with client() as c:
        r = await c.post("/auth/start", json={"email": "lena@example.com", "code": code, "name": "Лена"})
        req = r.json()["request"]
        for _ in range(2):  # an address not confirmed yet gets no letter, but it counts the same
            assert (await c.post("/auth/start", json={"email": "lena@example.com"})).status_code == 200
        assert (await c.post("/auth/start", json={"email": "lena@example.com"})).status_code == 429
        for _ in range(5):
            await c.post("/auth/code", json={"request": req, "code": "999999x"})
        digits, _ = letter_to("lena@example.com")
        assert (await c.post("/auth/code", json={"request": req, "code": digits})).status_code == 429


async def test_limit_per_client_address(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "login_per_ip_hour", 2)
    async with client() as c:
        for i in range(2):
            assert (await c.post("/auth/start", json={"email": f"ip{i}@example.com"})).status_code == 200
        assert (await c.post("/auth/start", json={"email": "ip9@example.com"})).status_code == 429


async def test_expired_letter_and_blocked_learner():
    async with client() as c:
        req = (await c.post("/auth/start", json={"email": "petya@example.com"})).json()["request"]
        _, link = letter_to("petya@example.com")
        async with sessionmaker()() as s:
            await s.execute(update(LoginRequest).where(LoginRequest.email == "petya@example.com")
                            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await s.commit()
        assert "устарела" in (await c.post("/auth/link", data={"t": link})).text
        assert (await c.post("/auth/poll", json={"request": req})).status_code == 410

        req = (await c.post("/auth/start", json={"email": "masha@example.com"})).json()["request"]
        digits, _ = letter_to("masha@example.com")
        async with sessionmaker()() as s:
            await s.execute(update(Learner).where(Learner.email == "masha@example.com").values(status="blocked"))
            await s.commit()
        assert (await c.post("/auth/code", json={"request": req, "code": digits})).status_code == 403
        assert (await c.get("/me", headers={"Authorization": "Bearer nonsense"})).status_code == 401


async def test_a_dev_server_takes_its_fixed_code_and_no_other_does(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "dev_login_code", "000000")
    code = await invite(name="Дев")
    async with client() as c:
        await c.post("/auth/start", json={"email": "dev@example.com", "code": code})
        assert letter_to("dev@example.com")[0] == "000000"
    from app import auth
    monkeypatch.setattr(settings, "public_url", "https://api.langlab.uk")  # reachable by anyone: random codes again
    assert auth._dev_code() is None
    monkeypatch.setattr(settings, "public_url", "http://127.0.0.1:8000")
    monkeypatch.setattr(settings, "dev_login_code", "0000")  # not six digits: not taken
    assert auth._dev_code() is None
