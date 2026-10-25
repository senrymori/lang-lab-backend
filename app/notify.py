"""Letters to every learner. The privacy policy changed: what changed, from when, and that agreeing needs nothing
while disagreeing is a letter to us — the account and its data are deleted (the policy's «Изменения»).

    uv run python -m app.notify policy --changes "…" [--from 2026-11-01] [--dry]
"""
from __future__ import annotations

import argparse
import asyncio
import html
from datetime import date

from sqlalchemy import select

from . import mail
from .config import settings
from .db import sessionmaker
from .models import Learner

CONTACT = "hello@langlab.uk"


def policy_letter(name: str, changes: str, start: date | None) -> tuple[str, str, str]:
    hello = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    when = f" Изменения вступают в силу {start.strftime('%d.%m.%Y')}." if start else ""
    subject = "Language Lab обновил политику конфиденциальности"
    text = (f"{hello}\n\nМы обновили политику конфиденциальности Language Lab.{when}\n\nЧто изменилось:\n{changes}\n\n"
            f"Полный текст: {settings.privacy_url}\n\nЕсли вы согласны — ничего делать не нужно. Если нет — напишите "
            f"на {CONTACT}, и мы удалим ваш аккаунт и все данные.")
    paras = "".join(f'<p style="font-size:16px">{html.escape(p)}</p>' for p in changes.split("\n") if p.strip())
    url = html.escape(settings.privacy_url)
    html_ = f"""<div style="font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;
padding:24px;color:#111">
<p style="font-size:16px">{html.escape(hello)}</p>
<p style="font-size:16px">Мы обновили политику конфиденциальности Language Lab.{html.escape(when)}</p>
<p style="font-size:16px;font-weight:600">Что изменилось:</p>{paras}
<p style="font-size:16px">Полный текст: <a href="{url}" style="color:#4116C9">{url}</a></p>
<p style="font-size:13px;color:#666">Если вы согласны — ничего делать не нужно. Если нет — напишите на
<a href="mailto:{CONTACT}" style="color:#666">{CONTACT}</a>, и мы удалим ваш аккаунт и все данные.</p></div>"""
    return subject, text, html_


async def policy(changes: str, start: date | None, dry: bool) -> int:
    """Every account, blocked ones too (their data is kept as well). Returns how many letters went."""
    async with sessionmaker()() as s:
        learners = list(await s.scalars(select(Learner).order_by(Learner.created_at)))
    if dry:
        subject, text, _ = policy_letter(learners[0].name if learners else "", changes, start)
        print(f"{len(learners)} писем. Тема: {subject}\n\n{text}")
        return 0
    for l in learners:
        await mail.send(l.email, *policy_letter(l.name, changes, start))
        if mail.backend() == "resend":
            await asyncio.sleep(0.6)  # Resend: a couple of letters a second at most
    return len(learners)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m app.notify")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("policy", help="the privacy policy changed: a letter to every learner")
    p.add_argument("--changes", required=True, help="what changed, in a few lines (a new line — a new paragraph)")
    p.add_argument("--from", dest="start", type=date.fromisoformat, help="when the change takes effect")
    p.add_argument("--dry", action="store_true", help="show the letter and how many go, send nothing")
    a = ap.parse_args()
    print("отправлено:", asyncio.run(policy(a.changes, a.start, a.dry)))
