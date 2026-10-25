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
    recordings_keep_days: int = 7  # voice recordings are deleted after this (app/cleanup.py)

    # where the server is reached from outside: links in letters point here
    public_url: str = "http://127.0.0.1:8000"
    # behind Cloudflare the client's address comes in CF-Connecting-IP (sign-in limits count by it)
    behind_cloudflare: bool = False

    # letters: resend | console | memory (tests); auto — resend when a key is found, else console
    mail_backend: str = "auto"
    mail_from: str = "LangLab <login@mail.langlab.uk>"
    privacy_url: str = "https://langlab.uk/privacy/"
    # the oldest app version the server still talks to (app/main.py: app_new_enough); empty — any
    min_app_version: str = ""  # the privacy policy: named in the letters that make an account
    download_url: str = ""  # where the app is downloaded from, for the invite letter (none yet: the letter skips it)
    resend_key: str | None = None
    resend_key_file: Path = Path.home() / ".config" / "lp" / "resend.key"

    # sign-in: the link and code live this long; a device stays signed in this long after its last visit
    login_minutes: int = 15
    session_days: int = 180
    login_per_email: int = 3  # letters per address within login_minutes
    # a dev server on this Mac (lang-lab-platform/scripts/client.sh): every sign-in code is this one, so nobody has to
    # dig the letter out of the log. Ignored unless public_url is this machine (auth._dev_code)
    dev_login_code: str | None = None
    login_per_ip_hour: int = 20

    # beta: registration closes at this many learners
    max_learners: int = 10
    # the tutor for learners, a model per mode (chosen by blind-judged runs); Claude stays for development
    tutor_provider: str = "openai"
    model_learn: str = "gpt-6.1-sol"
    model_placement: str = "gpt-6.1-sol"
    # the server's own key: a project allowed only these models, with a hard monthly limit
    openai_key: str | None = None
    openai_key_file: Path = Path.home() / ".config" / "lp" / "openai_server.key"
    # speech recognition (Gemini, paid tier: the free one trains on what it's sent)
    gemini_key: str | None = None
    gemini_key_file: Path = Path.home() / ".config" / "lp" / "gemini.key"
    # sign in with Google (app/google.py): a Web OAuth client whose redirect URI is <public_url>/auth/google/callback
    google_client_id: str = ""
    google_client_secret: str | None = None
    google_client_secret_file: Path = Path.home() / ".config" / "lp" / "google_client_secret.key"
    # the admin's API (app/admin.py): off without a key
    admin_key: str | None = None
    admin_key_file: Path = Path.home() / ".config" / "lp" / "admin.key"
    # access requests from the landing's form (app/intake.py): the site's worker sends them with this key; off without it
    intake_key: str | None = None
    intake_key_file: Path = Path.home() / ".config" / "lp" / "intake.key"
    admin_ui_dir: Path | None = None  # the admin page's build (lang-lab-admin/dist), served at /admin/
    # the admin's only host (Cloudflare Access in front): /admin is not found anywhere else, and this host serves
    # nothing but the admin. None (dev) — no split
    admin_host: str | None = None

    # spam that burns tokens, inside one live session (app/abuse.py): spend over any (minutes, USD) window
    # gets the session read by a model; after an "ok" it is read again only past this much more spend
    spike_windows: list[tuple[int, float]] = [(20, 2.0), (60, 4.0)]
    spike_recheck_usd: float = 2.0
    spike_review_model: str = "gpt-6.1-sol"
    # what learners write and say, checked by OpenAI's moderation (free): a flagged message is a red flag in the admin,
    # this many ever (self-harm aside — that is someone to help, never to block) blocks the learner
    moderation_model: str = "omni-moderation-latest"
    moderation_block_after: int = 3
    # free allowances the admin watches (they cost nothing until they run out): Azure's neural voices on the F0 tier,
    # Resend's free plan
    azure_free_chars_month: int = 500_000
    letters_day_limit: int = 100
    letters_month_limit: int = 3000
    # what LangLab costs every month regardless of lessons, only shown in the admin as a reminder (USD a month;
    # None — not set yet; until — when it runs out or renews):
    # LANGLAB_FIXED_COSTS='[{"name": "…", "usd_month": 12, "note": "…", "until": "2027-10-23"}]'
    fixed_costs: list[dict] = [
        {"name": "VPS OVHcloud", "usd_month": 28.70,
         "note": "VPS-4 (8 vCPU, 24 ГБ, 200 ГБ) $27.50 + автобэкап $1.20; продление ручное — переезд на VPS полегче",
         "until": "2026-11-23"},
        {"name": "Домен langlab.uk", "usd_month": 0.44, "note": "$5.30 в год, Cloudflare Registrar", "until": "2027-10-23"},
        {"name": "Cloudflare R2", "usd_month": 0, "note": "резервные копии и раздача приложения; бесплатно до 10 ГБ",
         "until": None},
        {"name": "Apple Developer Program", "usd_month": 8.25, "note": "$99 в год: подпись и нотаризация приложения",
         "until": None},
    ]
    # every few messages of the learner's own, a cheap model reads the lesson: still doing it, or gone off it
    drift_model: str = "gpt-6-luna"
    # abuse limits, enforced by the server whatever the client does
    audio_max_seconds: int = 120
    upload_max_mb: float = 10
    text_max_chars: int = 2000
    messages_per_minute: int = 20  # to the tutor, per lesson
    unanswered_max: int = 3  # the learner's own messages in a row the tutor hasn't answered; then they wait for it
    hear_per_minute: int = 12  # speech recognitions per learner (answers, voice notes, games)
    say_per_minute: int = 60  # lines voiced for a game per learner (the same line again is a file already made)


settings = Settings()
