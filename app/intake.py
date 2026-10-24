"""Access requests from the landing (langlab.uk): its worker checks Turnstile, then hands the request over here with
`X-Intake-Key` (LANGLAB_INTAKE_KEY or ~/.config/lp/intake.key; without a key intake is off). Nothing is mailed:
the admin reads them on «Заявки» and invites or dismisses."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from . import auth
from .config import settings
from .db import get_db
from .models import AccessRequest, Learner


def _key() -> str:
    f = settings.intake_key_file
    return settings.intake_key or (f.read_text().strip() if f.is_file() else "")


def require_site(request: Request) -> None:
    key = _key()
    if not key:
        raise HTTPException(503, "intake is off: no key")
    if not hmac.compare_digest(request.headers.get("x-intake-key", "").encode(), key.encode()):
        raise HTTPException(401)


router = APIRouter(prefix="/intake", dependencies=[Depends(require_site)])


class NewRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(max_length=320)


@router.post("/access-request")
async def access_request(body: NewRequest, s: AsyncSession = Depends(get_db)):
    """The same answer whatever happens: the site tells the person nothing about who is registered."""
    email, name = body.email.strip().lower(), " ".join(body.name.split())
    if not auth.EMAIL.match(email) or not name:
        raise HTTPException(400, "Проверь имя и адрес почты")
    if not (await s.execute(select(Learner.id).where(Learner.email == email))).first():
        await s.execute(insert(AccessRequest).values(name=name, email=email)
                        .on_conflict_do_update(index_elements=[AccessRequest.email], set_={"name": name}))
        await s.commit()
    return {"ok": True}
