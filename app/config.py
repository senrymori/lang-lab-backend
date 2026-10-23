"""Settings from the environment (LANGLAB_*), with defaults that run a dev server on this Mac as is."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LANGLAB_", env_file=".env", extra="ignore")

    # no URL: a Postgres of its own is started from var/pg (dev); on the server it is the compose service
    database_url: str | None = None
    # learner data as the desktop app keeps it (events, notes, practice), one folder per learner
    data_dir: Path = ROOT / "var" / "data"
    recordings_dir: Path = ROOT / "var" / "recordings"

    # where the server is reached from outside: links in letters point here
    public_url: str = "http://127.0.0.1:8000"
    # behind Cloudflare the client's address comes in CF-Connecting-IP (sign-in limits count by it)
    behind_cloudflare: bool = False

    # letters: resend | console | memory (tests); auto — resend when a key is found, else console
    mail_backend: str = "auto"
    mail_from: str = "Language Lab <login@mail.langlab.uk>"
    resend_key: str | None = None
    resend_key_file: Path = Path.home() / ".config" / "lp" / "resend.key"

    # sign-in: the link and code live this long; a device stays signed in this long after its last visit
    login_minutes: int = 15
    session_days: int = 180
    login_per_email: int = 3  # letters per address within login_minutes
    login_per_ip_hour: int = 20

    # beta: registration closes at this many learners
    max_learners: int = 10
    # the tutor for learners, a model per mode (chosen by blind-judged runs); Claude stays for development
    tutor_provider: str = "openai"
    model_learn: str = "gpt-6.1-sol"
    model_placement: str = "gpt-6-luna"
    model_drill: str = "gpt-6.1-sol"
    # the server's own key: a project allowed only these models, with a hard monthly limit
    openai_key: str | None = None
    openai_key_file: Path = Path.home() / ".config" / "lp" / "openai_server.key"

    # per learner and day (UTC): no new lesson or drill past the budget; a session already going may
    # finish on the grace, then it stops
    daily_budget_usd: float = 1.5
    budget_grace_usd: float = 0.5
    # abuse limits, enforced by the server whatever the client does
    audio_max_seconds: int = 120
    upload_max_mb: float = 10
    text_max_chars: int = 2000
    messages_per_minute: int = 20


settings = Settings()
