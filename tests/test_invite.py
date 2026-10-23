"""Alpha sign-up: the code comes first (the app checks it before offering Google or a letter), the name comes
from the code, a code made for an address is only for it, and the admin's mailed invite registers by its link."""
import re
import uuid

from app import auth, codes, mail
from app.db import sessionmaker
from app.models import AccessCode

from test_admin import admin  # noqa: F401  (fixture)
from test_auth import client, letter_to
from test_google import fake_google, run  # noqa: F401  (fixture)


def addr():
    return f"{uuid.uuid4().hex[:8]}@example.com"


def invite_link(email):
    m = [x for x in mail.outbox if x["to"] == [email]][-1]
    return re.search(r"/auth/invite\?t=(\S+)", m["text"])[1], re.search(r"код приглашения: (LAB-\S+)", m["text"])[1]


async def make(name="Аня", email=None):
    async with sessionmaker()() as s:
        return (await codes.create(s, note="тест", name=name, email=email)).code


async def test_the_code_is_checked_first_and_nothing_is_spent():
    code = await make()
    async with client() as c:
        r = await c.post("/auth/check-code", json={"code": code.lower().replace("-", " ")})
        assert r.status_code == 200 and r.json()["code"] == code
        assert (await c.post("/auth/check-code", json={"code": "LAB-NOPE-NOPE"})).status_code == 400
    async with sessionmaker()() as s:
        assert (await s.get(AccessCode, code)).used == 0


async def test_guessing_codes_is_cut_off():
    async with client() as c:
        got = [(await c.post("/auth/check-code", json={"code": "LAB-AAAA-AAAA"})).status_code
               for _ in range(auth.CHECKS_PER_HOUR + 1)]
    assert got[-1] == 429 and set(got[:-1]) == {400}


async def test_the_name_comes_from_the_code_not_the_learner():
    code, email = await make(name="Анна Сергеевна"), addr()
    async with client() as c:
        req = (await c.post("/auth/start", json={"email": email, "code": code, "name": "Кто-то"})).json()["request"]
        assert "Здравствуйте, Анна Сергеевна!" in mail.outbox[-1]["text"]
        r = await c.post("/auth/code", json={"request": req, "code": letter_to(email)[0]})
        assert r.status_code == 200 and r.json()["learner"]["name"] == "Анна Сергеевна"


async def test_a_code_made_for_an_address_lets_in_only_that_address(admin):  # noqa: F811
    email = addr()
    async with client() as c:
        code = (await c.post("/admin/api/codes", json={"name": "Боря", "email": email}, headers=admin)).json()["code"]
        assert (await c.post("/auth/check-code", json={"code": code})).status_code == 200  # address not known yet
        assert (await c.post("/auth/start", json={"email": addr(), "code": code})).status_code == 400
        req = (await c.post("/auth/start", json={"email": email, "code": code})).json()["request"]
        r = await c.post("/auth/code", json={"request": req, "code": letter_to(email)[0]})
        assert r.status_code == 200 and r.json()["learner"]["name"] == "Боря"


async def test_the_admin_mails_an_invite_and_its_link_registers(admin):  # noqa: F811
    email = addr()
    async with client() as c:
        r = await c.post("/admin/api/codes", json={"name": "Вера", "email": email.upper(), "lessons": 5}, headers=admin)
        assert r.status_code == 200 and r.json()["sent"] is True and r.json()["email"] == email
        t, code = invite_link(email)
        assert code == r.json()["code"]
        page = await c.get(f"/auth/invite?t={t}")  # opening the link does nothing but offer the app
        assert f"langlab://invite?t={t}" in page.text
        async with sessionmaker()() as s:
            assert (await s.get(AccessCode, code)).used == 0
        r = await c.post("/auth/invite", json={"t": t})
        assert r.status_code == 200, r.text
        me = r.json()["learner"]
        assert me["email"] == email and me["name"] == "Вера" and me["lessons_left"] == 5 and r.json()["token"]
        assert (await c.post("/auth/invite", json={"t": t})).status_code == 409  # used: signs nobody in again
        assert (await c.post("/admin/api/codes", json={"name": "Вера", "email": email}, headers=admin)).status_code == 409


async def test_sending_again_kills_the_old_link(admin):  # noqa: F811
    email = addr()
    async with client() as c:
        code = (await c.post("/admin/api/codes", json={"name": "Гоша", "email": email}, headers=admin)).json()["code"]
        old, _ = invite_link(email)
        assert (await c.post(f"/admin/api/codes/{code}/send", headers=admin)).status_code == 200
        new, _ = invite_link(email)
        assert old != new
        assert (await c.post("/auth/invite", json={"t": old})).status_code == 410
        assert (await c.post("/auth/invite", json={"t": new})).status_code == 200
        assert (await c.post(f"/admin/api/codes/{code}/send", headers=admin)).status_code == 400  # used


async def test_an_invite_needs_a_name_and_a_real_address(admin):  # noqa: F811
    async with client() as c:
        assert (await c.post("/admin/api/codes", json={"email": addr()}, headers=admin)).status_code == 400
        assert (await c.post("/admin/api/codes", json={"name": "Д", "email": "nope"}, headers=admin)).status_code == 400
        r = await c.post("/admin/api/codes", json={"kind": "promo", "max_uses": 50, "note": "канал"}, headers=admin)
        assert r.status_code == 200  # a promo code has no one person to name


async def test_google_registration_takes_the_code_name_and_its_address(fake_google):  # noqa: F811
    email = f"{uuid.uuid4().hex[:8]}@gmail.com"
    code = await make(name="Дина", email=email)
    async with client() as c:
        req, page = await run(c, fake_google, f"{uuid.uuid4().hex[:8]}@gmail.com", code=code)  # someone else's Google
        assert "Код не подошёл" in page
        req, page = await run(c, fake_google, email, code=code)
        r = await c.post("/auth/poll", json={"request": req})
        assert r.status_code == 200 and r.json()["learner"]["name"] == "Дина"
