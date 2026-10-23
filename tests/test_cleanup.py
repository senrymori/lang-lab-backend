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


def test_voiced_lines_unused_for_a_week_go(tmp_path, monkeypatch):
    import lp.tutor
    monkeypatch.setattr(settings, "recordings_dir", tmp_path / "rec")
    monkeypatch.setattr(lp.tutor, "MEDIA", tmp_path / "media")
    (tmp_path / "media").mkdir()
    old, used = tmp_path / "media" / "a.wav", tmp_path / "media" / "b.wav"
    for f in (old, used):
        f.write_bytes(b"x")
        week_ago = time.time() - 8 * 86400
        os.utime(f, (week_ago, week_ago))
    assert lp.tutor._reuse("b.wav")  # asked for again: touched
    assert cleanup.sweep() == 1
    assert used.exists() and not old.exists() and (tmp_path / "media").is_dir()


def test_the_network_guard_holds():
    import socket
    import pytest
    from tests.conftest import NetworkBlocked
    s = socket.socket()
    with pytest.raises(NetworkBlocked):
        s.connect(("api.openai.com", 443))
    s.close()
