import os
import time

from app import cleanup
from app.config import settings


def test_old_recordings_go_new_stay(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "recordings_dir", tmp_path)
    old = tmp_path / "u1" / "kk" / "2026-10-01" / "120000.webm"
    new = tmp_path / "u1" / "kk" / "2026-10-20" / "120000.webm"
    for f in (old, new):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
    week_ago = time.time() - 8 * 86400
    os.utime(old, (week_ago, week_ago))
    assert cleanup.sweep() == 1
    assert new.exists() and not old.exists() and not old.parent.exists()
    assert cleanup.sweep() == 0
