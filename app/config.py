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

    # beta: registration closes at this many learners
    max_learners: int = 10
    # the tutor for learners; Claude stays for development
    tutor_provider: str = "openai"
    tutor_model: str = "gpt-6.1-sol"
    # abuse limits, enforced by the server whatever the client does
    daily_budget_usd: float = 1.5
    audio_max_seconds: int = 120
    text_max_chars: int = 2000


settings = Settings()
