import base64
import json
import uuid

import httpx
import pytest

from app import codes, google
from app.config import settings
from app.db import sessionmaker

from test_hosting import client


def id_token(email, name="Гугл"):
    body = base64.urlsafe_b64encode(json.dumps({"email": email, "email_verified": True, "given_name": name}).encode())
    return f"x.{body.decode().rstrip('=')}.y"


@pytest.fixture
def fake_google(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "cid")
    monkeypatch.setattr(settings, "google_client_secret", "secret")
    who = {}

    class Fake:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, data):
            return httpx.Response(200, json={"id_token": id_token(who["email"])})
    monkeypatch.setattr(google, "AsyncClient", Fake)
    return who


async def run(c, who, email, **start):
    who["email"] = email
    r = (await c.post("/auth/google/start", json=start)).json()
    state = r["url"].split("state=")[1]
    assert (await c.get(f"/auth/google/go?state={state}")).status_code == 307
    page = await c.get(f"/auth/google/callback?state={state}&code=abc")
    return r["request"], page.text


async def test_new_learner_needs_a_code_then_signs_in(fake_google):
    email = f"{uuid.uuid4().hex[:8]}@gmail.com"
    async with client() as c:
        req, page = await run(c, fake_google, email)
        assert "код приглашения" in page
        assert (await c.post("/auth/poll", json={"request": req})).status_code == 410
        async with sessionmaker()() as s:
            code = (await codes.create(s, note="google")).code
        req, page = await run(c, fake_google, email, code=code)
        assert "langlab://signed-in" in page
        r = await c.post("/auth/poll", json={"request": req})
        assert r.status_code == 200 and r.json()["learner"]["name"] == "Гугл" and r.json()["learner"]["lessons_left"] == 12
        # next time: no code needed
        req, page = await run(c, fake_google, email)
        assert (await c.post("/auth/poll", json={"request": req})).status_code == 200


async def test_off_without_a_client(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "")
    async with client() as c:
        assert (await c.post("/auth/google/start", json={})).status_code == 503
