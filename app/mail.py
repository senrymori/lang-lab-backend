"""Letters: Resend on the server (and on this Mac, where the key is in ~/.config/lp/resend.key), the
console when there is no key. Tests switch to `memory` and read `outbox`."""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("langlab.mail")
outbox: list[dict] = []


class MailError(RuntimeError):
    pass


def _key() -> str | None:
    if settings.resend_key:
        return settings.resend_key
    f = settings.resend_key_file
    return f.read_text().strip() if f.is_file() else None


def backend() -> str:
    b = settings.mail_backend
    return ("resend" if _key() else "console") if b == "auto" else b


async def send(to: str, subject: str, text: str, html: str) -> None:
    msg = {"from": settings.mail_from, "to": [to], "subject": subject, "text": text, "html": html}
    b = backend()
    if b == "memory":
        outbox.append(msg)
    elif b == "console":
        log.warning("letter to %s — %s\n%s", to, subject, text)
    else:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://api.resend.com/emails", json=msg,
                             headers={"Authorization": f"Bearer {_key()}"})
        if r.status_code >= 300:
            log.error("resend %s: %s", r.status_code, r.text[:500])
            raise MailError(f"resend {r.status_code}")
