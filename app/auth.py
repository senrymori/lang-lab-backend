"""Sign-in by email — a link, or the six-digit code from the same letter — instead of a password.

In the alpha nobody gets in without an access code: the app asks for it first (`POST /auth/check-code`) and
only then offers Google or a letter; a learner who already has an account signs in without one. The one
exception is an invite the admin mails (`invite_letter`): its link registers them with nothing to type.

The app sends `POST /auth/start` with the address (plus the access code to register) and gets
a request token only it knows. The link in the letter opens a page with a button: a GET never signs
anyone in, so mail scanners that open links do nothing. The button approves the request and the app,
polling `POST /auth/poll`, gets its session — the letter can be opened on the phone while the app waits
on the Mac. Typing the code into the app (`POST /auth/code`) does the same in one step. A registration
spends the access code only at that point.

The answer to /auth/start is the same whether the address is known or not; letters are limited per
address and per client address. The app then sends `Authorization: Bearer <token>`; a page (and its
<audio> and WebSocket, which can't set headers) has the same token in an HttpOnly cookie."""
from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import HTTPConnection
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import codes, mail
from .config import settings
from .db import get_db
from .models import AccessCode, AuthSession, Learner, LoginRequest

router = APIRouter()

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_ATTEMPTS = 5
FINISH_WITHIN = timedelta(minutes=10)  # after the button is pressed, the app has this long to pick it up
EXPIRED = "Ссылка и код устарели — запросите новое письмо"
BLOCKED = "Доступ к LangLab закрыт — напишите нам"
COOKIE = "langlab_session"


def _token() -> str:
    return secrets.token_urlsafe(32)


def _hash(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ip(request: Request) -> str:
    if settings.behind_cloudflare and (ip := request.headers.get("cf-connecting-ip")):
        return ip[:64]
    return request.client.host if request.client else ""


def learner_json(learner: Learner) -> dict:
    until = learner.access_until
    left = max(0, learner.lessons_total - learner.lessons_used)
    return {"id": str(learner.id), "email": learner.email, "name": learner.name, "status": learner.status,
            "access_until": until.astimezone(timezone.utc).isoformat() if until else None,
            "lessons_total": learner.lessons_total, "lessons_left": left,
            "access": learner.status == "active" and (until is None or until > _now())}


# --- the letter and the page behind its link ---

def _dev_code() -> str | None:
    """The fixed sign-in code of a dev server, only when it is reached at this machine: a server anyone can reach
    never takes it, whatever its environment says."""
    code = settings.dev_login_code
    local = urlsplit(settings.public_url).hostname in ("127.0.0.1", "localhost", "::1")
    return code if code and local and re.fullmatch(r"\d{6}", code) else None


# an account is made by signing in from a letter: the letter says what that means
CONSENT = "Входя в приложение, вы создаёте аккаунт и принимаете политику конфиденциальности"


def _consent_html() -> str:
    url = html.escape(settings.privacy_url)
    return f'<p style="font-size:13px;color:#666">{CONSENT}: <a href="{url}" style="color:#666">{url}</a></p>'


def _letter(purpose: str, name: str, digits: str, link: str) -> tuple[str, str, str]:
    hello = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    what = "подтвердить адрес и начать заниматься" if purpose == "register" else "войти в LangLab"
    new = purpose == "register"
    subject = f"Код для входа в LangLab: {digits}"
    text = (f"{hello}\n\nЧтобы {what}, откройте ссылку и нажмите «Войти»:\n{link}\n\n"
            f"Или введите в приложении код: {digits}\n\n"
            f"Ссылка и код действуют {settings.login_minutes} минут. Если вы не входили в LangLab — "
            "просто не обращайте внимания на это письмо."
            + (f"\n\n{CONSENT}: {settings.privacy_url}" if new else ""))
    html_ = f"""<div style="font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;
padding:24px;color:#111">
<p style="font-size:16px">{html.escape(hello)}</p>
<p style="font-size:16px">Чтобы {what}, нажмите кнопку:</p>
<p><a href="{html.escape(link)}" style="display:inline-block;background:#4116C9;color:#fff;text-decoration:none;
padding:12px 28px;border-radius:14px;font-weight:600;font-size:16px">Войти</a></p>
<p style="font-size:16px">Или введите в приложении код:</p>
<p style="font-size:32px;font-weight:700;letter-spacing:6px;margin:8px 0 24px">{digits}</p>
<p style="font-size:13px;color:#666">Ссылка и код действуют {settings.login_minutes} минут. Если вы не входили
в LangLab — просто не обращайте внимания на это письмо.</p>{_consent_html() if new else ""}</div>"""
    return subject, text, html_


APP_LINK = "langlab://signed-in"  # the desktop app's scheme (lp desktop/main.js); it carries nothing secret


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} — LangLab</title>
<style>
:root{{--bg:#000;--card:rgba(44,44,52,.6);--text:#fff;--muted:#9a9aa6;--accent:#4116C9}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);
color:var(--text);font:16px/1.5 Inter,-apple-system,Segoe UI,Roboto,sans-serif}}
.card{{background:var(--card);border-radius:14px;padding:32px;margin:16px;max-width:400px;text-align:center}}
h1{{font-size:22px;margin:0 0 12px}} p{{color:var(--muted);margin:0 0 20px}}
button,a.btn{{display:inline-block;background:var(--accent);color:#fff;border:0;border-radius:14px;padding:12px 32px;
font:600 16px Inter,sans-serif;cursor:pointer;text-decoration:none}}
</style></head><body><div class="card"><h1>{title}</h1>{body}</div></body></html>""")


# --- endpoints ---

class Start(BaseModel):
    email: str = Field(max_length=320)
    code: str | None = Field(None, max_length=40)  # an access code: registers a new learner (named by the code)


class CheckCode(BaseModel):
    code: str = Field(max_length=40)


class Invite(BaseModel):
    t: str = Field(max_length=100)


CHECKS_PER_HOUR = 20
_checks: dict[str, list[datetime]] = {}  # client address -> recent code checks (one server process)


@router.post("/auth/check-code")
async def check_code(body: CheckCode, request: Request, s: AsyncSession = Depends(get_db)):
    """The app's first screen: is this a code that lets a new learner in? Nothing is spent. Limited per address,
    so codes can't be guessed by trying."""
    now, ip = _now(), _ip(request)
    recent = [t for t in _checks.get(ip, []) if t > now - timedelta(hours=1)]
    if len(recent) >= CHECKS_PER_HOUR:
        raise HTTPException(429, "Слишком много попыток — попробуйте через час")
    _checks[ip] = recent + [now]
    try:
        await codes.check(s, body.code, new_learner=True, any_address=True)
    except codes.CodeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "code": codes.normalize(body.code)}


class Poll(BaseModel):
    request: str = Field(max_length=100)


class Code(Poll):
    code: str = Field(max_length=20)


@router.post("/auth/start")
async def start(body: Start, request: Request, s: AsyncSession = Depends(get_db)):
    email = body.email.strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(400, "Проверьте адрес почты")
    now, ip = _now(), _ip(request)
    await s.execute(delete(LoginRequest).where(LoginRequest.created_at < now - timedelta(days=1)))
    recent = select(func.count()).select_from(LoginRequest)
    # letters nobody signed in by: one used to sign in is no spam, so signing out and back in isn't held up
    if (await s.execute(recent.where(LoginRequest.email == email, LoginRequest.consumed_at.is_(None),
                                     LoginRequest.created_at > now - timedelta(minutes=settings.login_minutes)))).scalar_one() \
            >= settings.login_per_email:
        raise HTTPException(429, "Письмо уже отправлено — проверьте почту и папку «Спам». "
                                 "Новое можно запросить через несколько минут")
    if ip and (await s.execute(recent.where(LoginRequest.ip == ip, LoginRequest.created_at >
                                            now - timedelta(hours=1)))).scalar_one() >= settings.login_per_ip_hour:
        raise HTTPException(429, "Слишком много попыток входа — попробуйте через час")

    known = (await s.execute(select(Learner).where(Learner.email == email))).scalar_one_or_none()
    try:
        if body.code:  # checked even for a known address, so a wrong code says nothing about who is registered
            await codes.check(s, body.code, new_learner=known is None, email=email)
    except codes.CodeError as e:
        raise HTTPException(400, str(e))
    if known:
        purpose, name = "login", known.name
    elif body.code:
        purpose, name = "register", await codes.name_of(s, body.code)
    else:
        purpose, name = "none", ""

    req, link, digits = _token(), _token(), _dev_code() or f"{secrets.randbelow(10 ** 6):06d}"
    rid = _hash(req)
    s.add(LoginRequest(id=rid, link=_hash(link), email=email, purpose=purpose, name=name[:80], ip=ip,
                       code=_hash(f"{rid}:{digits}") if purpose != "none" else _hash(_token()),
                       access_code=codes.normalize(body.code) if purpose == "register" else None,
                       expires_at=now + timedelta(minutes=settings.login_minutes)))
    await s.commit()
    if purpose != "none":
        try:
            await mail.send(email, *_letter(purpose, name, digits,
                                            f"{settings.public_url.rstrip('/')}/auth/link?t={link}"))
        except mail.MailError:
            raise HTTPException(502, "Не получилось отправить письмо — попробуйте чуть позже")
    return {"request": req, "expires_in": settings.login_minutes * 60}


@router.get("/auth/link", response_class=HTMLResponse)
async def link_page(t: str = ""):
    """Only a button: opening the link changes nothing."""
    return _page("Вход в LangLab", f"""<p>Нажмите, чтобы войти в приложении.</p>
<form method="post" action="/auth/link"><input type="hidden" name="t" value="{html.escape(t)}">
<button type="submit">Войти</button></form>""")


@router.post("/auth/link", response_class=HTMLResponse)
async def link_confirm(t: str = Form(""), s: AsyncSession = Depends(get_db)):
    now = _now()
    ok = (await s.execute(update(LoginRequest)
                          .where(LoginRequest.link == _hash(t), LoginRequest.purpose != "none",
                                 LoginRequest.approved_at.is_(None), LoginRequest.expires_at > now)
                          .values(approved_at=now).returning(LoginRequest.id))).first()
    await s.commit()
    if ok:  # the app's own link brings it to the front (or starts it), and it finishes signing in by itself
        return _page("Готово", f"""<p>Открываем LangLab — вход выполнится сам.</p>
<a class="btn" href="{APP_LINK}">Открыть приложение</a><script>location.href = "{APP_LINK}"</script>""")
    r = (await s.execute(select(LoginRequest).where(LoginRequest.link == _hash(t)))).scalar_one_or_none()
    if r and r.approved_at:
        return _page("Вход уже подтверждён", "<p>Вернитесь в приложение LangLab.</p>")
    return _page("Ссылка устарела", "<p>Запросите новое письмо в приложении.</p>")


@router.post("/auth/poll")
async def poll(body: Poll, request: Request, s: AsyncSession = Depends(get_db)):
    """202 while the letter waits, 410 when it's over, 200 with the session once the link was confirmed."""
    r = await s.get(LoginRequest, _hash(body.request))
    if r and r.approved_at and not r.consumed_at:
        return await _finish(s, r.id, request)
    if r is None or r.consumed_at or r.expires_at <= _now():
        raise HTTPException(410, EXPIRED)
    return JSONResponse({"status": "pending"}, status_code=202)


@router.post("/auth/code")
async def enter_code(body: Code, request: Request, s: AsyncSession = Depends(get_db)):
    rid, now = _hash(body.request), _now()
    stored = (await s.execute(update(LoginRequest)
                              .where(LoginRequest.id == rid, LoginRequest.consumed_at.is_(None),
                                     LoginRequest.expires_at > now, LoginRequest.attempts < MAX_ATTEMPTS)
                              .values(attempts=LoginRequest.attempts + 1)
                              .returning(LoginRequest.code))).scalar_one_or_none()
    await s.commit()
    if stored is None:
        r = await s.get(LoginRequest, rid)
        if r and not r.consumed_at and r.expires_at > now:
            raise HTTPException(429, "Слишком много неверных кодов — запросите новое письмо")
        raise HTTPException(410, EXPIRED)
    digits = re.sub(r"\D", "", body.code)
    if not hmac.compare_digest(stored, _hash(f"{rid}:{digits}")):
        raise HTTPException(400, "Код не тот — проверьте письмо")
    await s.execute(update(LoginRequest).where(LoginRequest.id == rid, LoginRequest.approved_at.is_(None))
                    .values(approved_at=now))
    await s.commit()
    return await _finish(s, rid, request)


async def _finish(s: AsyncSession, rid: str, request: Request) -> dict:
    """Spend the approved request once (two polls can't both get a session), register if it's a
    registration, open a session."""
    now = _now()
    r = (await s.execute(update(LoginRequest)
                         .where(LoginRequest.id == rid, LoginRequest.consumed_at.is_(None),
                                LoginRequest.purpose != "none", LoginRequest.approved_at > now - FINISH_WITHIN)
                         .values(consumed_at=now)
                         .returning(LoginRequest.purpose, LoginRequest.email, LoginRequest.name,
                                    LoginRequest.access_code))).first()
    await s.commit()
    if r is None:
        raise HTTPException(410, EXPIRED)
    if r.purpose == "register":
        try:
            learner = await codes.register(s, email=r.email, code=r.access_code, name=r.name)
        except codes.CodeError as e:  # the code went to someone else or the beta filled up meanwhile
            raise HTTPException(409, str(e))
    else:
        learner = (await s.execute(select(Learner).where(Learner.email == r.email))).scalar_one()
    return await _open(s, learner, request)


async def _open(s: AsyncSession, learner: Learner, request: Request) -> JSONResponse:
    """A session for this device: the token to the app, the same in an HttpOnly cookie for a page."""
    if learner.status != "active":
        raise HTTPException(403, BLOCKED)
    token = _token()
    s.add(AuthSession(id=_hash(token), learner_id=learner.id,
                      device=request.headers.get("user-agent", "")[:200]))
    await s.commit()
    resp = JSONResponse({"status": "ok", "token": token, "learner": learner_json(learner)})
    resp.set_cookie(COOKIE, token, max_age=settings.session_days * 86400, httponly=True, samesite="lax",
                    secure=settings.public_url.startswith("https://"))
    return resp


# --- an invite mailed from the admin: its link registers them, no code to type ---

LANGUAGES = "английского, испанского, итальянского и казахского"
INVITE_LINK = "langlab://invite?t="  # handled by the app's main process (lp desktop/main.js), never by its page


def invite_letter(name: str, code: str, link: str) -> tuple[str, str, str]:
    """`link` — our page, which hands the token to the app; `code` — the same invite typed by hand."""
    hello = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    get = f"Скачайте приложение: {settings.download_url}\n" if settings.download_url else ""
    subject = "Приглашение в LangLab"
    text = (f"{hello}\n\nВас пригласили в LangLab — репетитора {LANGUAGES}.\n\n"
            f"{get}Чтобы начать, откройте ссылку на этом Mac и нажмите «Открыть LangLab»:\n{link}\n\n"
            f"Или откройте приложение и введите код приглашения: {code}\nВойти нужно с этой почты.\n\n"
            f"{CONSENT}: {settings.privacy_url}")
    dl = (f'<p style="font-size:16px">Сначала скачайте приложение: <a href="{html.escape(settings.download_url)}" '
          f'style="color:#4116C9">{html.escape(settings.download_url)}</a></p>') if settings.download_url else ""
    html_ = f"""<div style="font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;
padding:24px;color:#111">
<p style="font-size:16px">{html.escape(hello)}</p>
<p style="font-size:16px">Вас пригласили в LangLab — репетитора {LANGUAGES}.</p>{dl}
<p style="font-size:16px">Откройте на своём Mac:</p>
<p><a href="{html.escape(link)}" style="display:inline-block;background:#4116C9;color:#fff;text-decoration:none;
padding:12px 28px;border-radius:14px;font-weight:600;font-size:16px">Начать</a></p>
<p style="font-size:16px">Или откройте приложение и введите код приглашения:</p>
<p style="font-size:24px;font-weight:700;letter-spacing:3px;margin:8px 0 24px">{code}</p>
<p style="font-size:13px;color:#666">Войти нужно с этой почты. Приглашение личное — не пересылайте его.</p>
{_consent_html()}</div>"""
    return subject, text, html_


@router.get("/auth/invite", response_class=HTMLResponse)
async def invite_page(t: str = ""):
    """Opening the letter's link changes nothing (mail scanners open links): the button hands the token to the app."""
    get = (f'<p>Нет приложения? <a href="{html.escape(settings.download_url)}" style="color:#a28bff">Скачайте его</a>, '
           "установите и нажмите кнопку ещё раз.</p>") if settings.download_url else \
        "<p>Если ничего не открылось — установите приложение и нажмите ещё раз.</p>"
    return _page("Приглашение в LangLab", f"""<p>Нажмите, чтобы открыть приложение и начать.</p>
<a class="btn" href="{INVITE_LINK}{html.escape(t)}">Открыть LangLab</a>{get}""")


@router.post("/auth/invite")
async def redeem_invite(body: Invite, request: Request, s: AsyncSession = Depends(get_db)):
    """The app got the letter's token: register the address the invite was made for and open a session. The link
    dies with the code: once used it signs nobody in again."""
    c = (await s.execute(select(AccessCode).where(AccessCode.link == _hash(body.t)))).scalar_one_or_none()
    if c is None or not c.email:
        raise HTTPException(410, "Приглашение не найдено — попросите прислать новое")
    if (await s.execute(select(Learner.id).where(Learner.email == c.email))).first():
        raise HTTPException(409, "Вы уже зарегистрированы — войдите по почте")
    await s.commit()  # register runs its own transaction
    try:
        learner = await codes.register(s, email=c.email, code=c.code)
    except codes.CodeError as e:
        raise HTTPException(410, f"{e}. Попросите прислать новое приглашение")
    return await _open(s, learner, request)


# --- the signed-in learner ---

def token_of(conn: HTTPConnection) -> str:
    """The bearer header (the app), else the cookie (a page). Never the URL: URLs land in logs — a WebSocket
    that can't send either takes a one-time ticket instead (`/auth/ws-ticket`)."""
    auth = conn.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return conn.cookies.get(COOKIE) or ""


# one-time tickets for a WebSocket (`?ticket=`): good for one connection within TICKET_SECONDS, so the one a log
# keeps is already spent. In memory: one server process
TICKET_SECONDS = 60
_tickets: dict[str, tuple[str, datetime]] = {}  # sha256 of the ticket -> (device session id, valid until)


async def by_ticket(s: AsyncSession, ticket: str) -> tuple[AuthSession, Learner] | None:
    """The device session a WebSocket's ticket was issued to; the ticket is spent either way."""
    got = _tickets.pop(_hash(ticket), None) if ticket else None
    if got is None or got[1] < _now():
        return None
    return await _session(s, got[0])


async def signed_in(s: AsyncSession, token: str) -> tuple[AuthSession, Learner] | None:
    """The device session behind a token and its learner; None if unknown, revoked or idle too long."""
    return await _session(s, _hash(token)) if token else None


async def _session(s: AsyncSession, sid: str) -> tuple[AuthSession, Learner] | None:
    row = await s.get(AuthSession, sid)
    now = _now()
    if row is None or row.revoked_at or row.last_seen_at < now - timedelta(days=settings.session_days):
        return None
    learner = await s.get(Learner, row.learner_id)
    if learner is None:
        return None
    if now - row.last_seen_at > timedelta(hours=1):
        row.last_seen_at = now
        await s.commit()
    return row, learner


async def current_learner(request: Request, s: AsyncSession = Depends(get_db)) -> Learner:
    """Dependency for everything behind sign-in. A blocked learner is out at once; an expired access
    still signs in (the app shows it), lessons check `access` themselves."""
    found = await signed_in(s, token_of(request))
    if found is None:
        raise HTTPException(401, "Войдите заново", headers={"WWW-Authenticate": "Bearer"})
    row, learner = found
    if learner.status != "active":
        raise HTTPException(403, BLOCKED)
    request.state.session_id = row.id
    return learner


@router.post("/auth/ws-ticket")
async def ws_ticket(request: Request, _: Learner = Depends(current_learner)):
    now = _now()
    for k in [k for k, (_, until) in _tickets.items() if until < now]:
        del _tickets[k]
    ticket = _token()
    _tickets[_hash(ticket)] = (request.state.session_id, now + timedelta(seconds=TICKET_SECONDS))
    return {"ticket": ticket, "expires_in": TICKET_SECONDS}


@router.get("/me")
async def me(learner: Learner = Depends(current_learner)):
    return learner_json(learner)


@router.post("/auth/logout")
async def logout(request: Request, s: AsyncSession = Depends(get_db)):
    """Signing out works for anyone signed in — a blocked learner too (from the ban screen)."""
    found = await signed_in(s, token_of(request))
    if found is None:
        raise HTTPException(401, "Войдите заново", headers={"WWW-Authenticate": "Bearer"})
    await s.execute(update(AuthSession).where(AuthSession.id == found[0].id).values(revoked_at=_now()))
    await s.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp
