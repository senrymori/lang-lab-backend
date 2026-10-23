"""Every test run gets a Postgres and data folders of its own; the dev ones in var/ are never touched. It runs
offline: no request may leave the machine (a model call costs money — any attempt fails the test), and the home
folder is a temp one, so no real key (~/.config/lp) is ever loaded."""
import os
import pathlib
import socket
import tempfile

import pgserver

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="langlab-test-"))
(_tmp / "home").mkdir()
os.environ["HOME"] = str(_tmp / "home")
for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "AZURE_SPEECH_KEY"):
    os.environ.pop(k, None)
_pg = pgserver.get_server(str(_tmp / "pg"), cleanup_mode="delete")
os.environ["LANGLAB_DATABASE_URL"] = _pg.get_uri()
os.environ["LANGLAB_DATA_DIR"] = str(_tmp / "data")
os.environ["LANGLAB_MAIL_BACKEND"] = "memory"  # letters land in app.mail.outbox, nothing is sent
os.environ["LP_DATA"] = str(_tmp / "lp")  # the learning core never writes real learner data in tests

_connect = socket.socket.connect


class NetworkBlocked(RuntimeError):
    pass


def _offline(self, addr):
    host = addr[0] if isinstance(addr, tuple) else addr
    if isinstance(host, str) and (host.startswith("127.") or host in ("::1", "localhost") or host.startswith("/")):
        return _connect(self, addr)
    raise NetworkBlocked(f"a test tried to reach {addr}: stub that layer — real calls cost money")


socket.socket.connect = _offline

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

command.upgrade(Config(str(pathlib.Path(__file__).resolve().parent.parent / "alembic.ini")), "head")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def roomy(monkeypatch):
    """Every test registers learners into one database: the beta cap would close it midway (the cap has
    its own test, which sets its own)."""
    from app.config import settings
    monkeypatch.setattr(settings, "max_learners", 1000)
