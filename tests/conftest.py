"""Every test run gets a Postgres and data folders of its own; the dev ones in var/ are never touched."""
import os
import pathlib
import tempfile

import pgserver

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="langlab-test-"))
_pg = pgserver.get_server(str(_tmp / "pg"), cleanup_mode="delete")
os.environ["LANGLAB_DATABASE_URL"] = _pg.get_uri()
os.environ["LANGLAB_DATA_DIR"] = str(_tmp / "data")
os.environ["LANGLAB_MAIL_BACKEND"] = "memory"  # letters land in app.mail.outbox, nothing is sent
os.environ["LP_DATA"] = str(_tmp / "lp")  # the learning core never writes real learner data in tests

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
