"""Learners' voice recordings are kept a week. They serve the lesson itself (▶ next to an answer, the
pronunciation check); the tutor goes on with the text. The week is for looking into «it heard me wrong».
The voiced lines (lp's media folder: the tutor's speech, game lines, song previews) go after a week unused
too: the same line asked again touches its file, and one gone is made again when asked."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .config import settings


def sweep(now: float | None = None) -> int:
    """Delete recordings and voiced lines older than `recordings_keep_days`, then the folders left empty.
    Returns files deleted."""
    from lp.tutor import MEDIA
    cutoff = (now or time.time()) - settings.recordings_keep_days * 86400
    return _sweep(settings.recordings_dir, cutoff) + _sweep(MEDIA, cutoff)


def _sweep(root: Path, cutoff: float) -> int:
    if not root.is_dir():
        return 0
    old = [f for f in root.rglob("*") if f.is_file() and f.stat().st_mtime < cutoff]
    for f in old:
        f.unlink(missing_ok=True)
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    return len(old)


async def every(hours: float = 6) -> None:
    while True:
        await asyncio.to_thread(sweep)
        await asyncio.sleep(hours * 3600)
