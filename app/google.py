"""Sign in with Google: the same request the app polls as for a letter (app/auth.py), only Google confirms the
address instead of a link in the mail.

The app asks for a request (POST /auth/google/start, with an access code when it's a new learner), opens the
returned URL in the browser; we send the browser to Google, Google back to /auth/google/callback with the
verified address; the request is approved for it and the page wakes the app (langlab://) to pick up the session
with /auth/poll. A new address still needs an access code: Google only says who it is, not that they're invited.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
from datetime import timedelta

from httpx import AsyncClient
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import codes
from .auth import APP_LINK, _hash, _ip, _now, _page, _token
from .config import settings
from .db import get_db
from .models import Learner, LoginRequest

router = APIRouter()
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"


def _secret() -> str:
    f = settings.google_client_secret_file
    return settings.google_client_secret or (f.read_text().strip() if f.is_file() else "")


def _redirect_uri() -> str:
    return f"{settings.public_url.rstrip('/')}/auth/google/callback"


class GoogleStart(BaseModel):
    code: str | None = Field(None, max_length=40)  # an access code: a new learner (named by the code)


@router.post("/auth/google/start")
async def google_start(body: GoogleStart, request: Request, s: AsyncSession = Depends(get_db)):
    if not settings.google_client_id or not _secret():
        raise HTTPException(503, "Вход через Google пока не настроен — войдите по почте")
    now, ip = _now(), _ip(request)
    if ip and (await s.execute(select(func.count()).select_from(LoginRequest).where(
            LoginRequest.ip == ip, LoginRequest.created_at > now - timedelta(hours=1)))).scalar_one() \
            >= settings.login_per_ip_hour:
        raise HTTPException(429, "Слишком много попыток входа — попробуйте через час")
    if body.code:
        try:
            await codes.check(s, body.code, new_learner=True, any_address=True)
        except codes.CodeError as e:
            raise HTTPException(400, str(e))
    req, state = _token(), _token()
    s.add(LoginRequest(id=_hash(req), link=_hash(state), code=_hash(_token()), email="", purpose="google",
                       ip=ip,
                       access_code=codes.normalize(body.code) if body.code else None,
                       expires_at=now + timedelta(minutes=settings.login_minutes)))
    await s.commit()
    return {"request": req, "expires_in": settings.login_minutes * 60,
            "url": f"{settings.public_url.rstrip('/')}/auth/google/go?state={state}"}


@router.get("/auth/google/go")
async def google_go(state: str = ""):
    """The browser's way to Google (the client id stays here, the app only knows our URL)."""
    q = {"client_id": settings.google_client_id, "redirect_uri": _redirect_uri(), "response_type": "code",
         "scope": "openid email profile", "state": state, "prompt": "select_account"}
    return RedirectResponse(f"{AUTH}?{urllib.parse.urlencode(q)}")


def _claims(id_token: str) -> dict:
    """The ID token's payload. It came to us from Google itself over TLS, in exchange for the code: no signature
    check is needed for a token we fetched (OpenID Connect Core 3.1.3.7)."""
    payload = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


@router.get("/auth/google/callback", response_class=HTMLResponse)
async def google_callback(state: str = "", code: str = "", error: str = "", s: AsyncSession = Depends(get_db)):
    now = _now()
    r = (await s.execute(select(LoginRequest).where(LoginRequest.link == _hash(state), LoginRequest.purpose == "google",
                                                    LoginRequest.approved_at.is_(None),
                                                    LoginRequest.expires_at > now))).scalar_one_or_none()
    if r is None:
        return _page("Ссылка устарела", "<p>Вернитесь в приложение и нажмите «Войти через Google» ещё раз.</p>")
    if error or not code:
        return _page("Вход отменён", "<p>Вернитесь в приложение — можно войти по почте.</p>")
    async with AsyncClient(timeout=20) as c:
        t = await c.post(TOKEN, data={"code": code, "client_id": settings.google_client_id, "client_secret": _secret(),
                                      "redirect_uri": _redirect_uri(), "grant_type": "authorization_code"})
    if t.status_code != 200:
        return _page("Не получилось", "<p>Google не подтвердил вход. Попробуйте ещё раз или войдите по почте.</p>")
    who = _claims(t.json()["id_token"])
    email = str(who.get("email", "")).strip().lower()
    if not email or not who.get("email_verified"):
        return _page("Не получилось", "<p>У этого Google-аккаунта нет подтверждённой почты — войдите по почте.</p>")

    known = (await s.execute(select(Learner).where(Learner.email == email))).scalar_one_or_none()
    if known:
        purpose, name = "login", known.name
    elif r.access_code:
        try:
            await codes.check(s, r.access_code, new_learner=True, email=email)
        except codes.CodeError as e:
            return _page("Код не подошёл", f"<p>{e}. Если приглашение пришло на другую почту — войдите с неё. "
                                          "Вернитесь в приложение.</p>")
        # the code's name wins at registration; Google's is only for a code without one
        purpose, name = "register", who.get("given_name") or who.get("name") or ""
    else:
        await s.execute(update(LoginRequest).where(LoginRequest.id == r.id).values(expires_at=now))
        await s.commit()
        return _page("Сначала код приглашения", f"""<p>{email} ещё не зарегистрирован в LangLab. Вернитесь в
приложение и сначала введите код приглашения.</p>""")
    await s.execute(update(LoginRequest).where(LoginRequest.id == r.id)
                    .values(email=email, purpose=purpose, name=str(name)[:80], approved_at=now))
    await s.commit()
    return _page("Готово", f"""<p>Открываем LangLab — вход выполнится сам.</p>
<a class="btn" href="{APP_LINK}">Открыть приложение</a><script>location.href = "{APP_LINK}"</script>""")

