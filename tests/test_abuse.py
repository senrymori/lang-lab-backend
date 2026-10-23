from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import abuse, hosting
from app.db import sessionmaker
from app.models import Learner, SpendAlert
from tests.test_hosting import learner


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(abuse, "_live", {})  # each test's sessions alone (real sids never repeat)
    now = [1000.0]
    monkeypatch.setattr(abuse, "_clock", lambda: now[0])
    return now


@pytest.fixture
def verdicts(monkeypatch):
    """The model's readings, in order; each reading taken is recorded."""
    out, seen = [], []

    async def review(S, sums):
        seen.append(sums)
        return out.pop(0)
    monkeypatch.setattr(abuse, "_review", review)
    return out, seen


def session(l, sid="s1"):
    return SimpleNamespace(user=str(l.id), sid=sid, lang="en", mode="learn", log=[])


async def alerts(l):
    async with sessionmaker()() as s:
        return (await s.execute(select(SpendAlert).where(SpendAlert.learner_id == l.id))).scalars().all()


async def test_a_lesson_spending_slowly_is_never_read(clock, verdicts):
    _, l = await learner("Марат")
    S = session(l)
    for _ in range(40):  # $4 over 80 min: a long real lesson
        await hosting.check(S)
        await hosting.book(S, 0.1)
        clock[0] += 120
    assert verdicts[1] == [] and await alerts(l) == []


async def test_spam_blocks_the_learner_and_stops_the_lesson(clock, verdicts):
    _, l = await learner("Спамер")
    S = session(l)
    verdicts[0].append({"spam": True, "reason": "шлёт одно и то же"})
    for _ in range(20):  # $2 in 10 min
        await hosting.book(S, 0.1)
        clock[0] += 30
    with pytest.raises(RuntimeError, match="приостановлен"):
        await hosting.check(S)
    async with sessionmaker()() as s:
        got = await s.get(Learner, l.id)
    assert got.status == "blocked" and "шлёт" in got.blocked_reason
    [a] = await alerts(l)
    assert a.verdict == "spam" and a.usd_by_window["20"] == pytest.approx(2.0)


async def test_a_real_lesson_goes_on_and_is_read_again_only_after_more_spend(clock, verdicts):
    _, l = await learner("Болтун")
    S = session(l)
    verdicts[0].extend([{"spam": False, "reason": "много говорит по делу"}, {"spam": False, "reason": "всё ещё урок"}])
    for _ in range(25):
        await hosting.book(S, 0.1)
        await hosting.check(S)
    assert len(verdicts[1]) == 1  # $2.5 so far: read once, at $2
    for _ in range(20):
        await hosting.book(S, 0.1)
        await hosting.check(S)
    assert len(verdicts[1]) == 2  # read again past $4
    async with sessionmaker()() as s:
        assert (await s.get(Learner, l.id)).status == "active"


async def test_the_window_is_one_session_not_the_learner(clock, verdicts):
    _, l = await learner("Четыре урока")
    for n in range(4):  # $1.5 each, back to back, within an hour
        S = session(l, sid=f"lesson{n}")
        for _ in range(15):
            await hosting.book(S, 0.1)
            await hosting.check(S)
            clock[0] += 30
    assert verdicts[1] == []


async def test_a_failed_reading_lets_the_lesson_go_on(clock, monkeypatch):
    _, l = await learner("Невезучий")
    S = session(l)

    async def broken(S, sums):
        raise TimeoutError("нет ответа")
    monkeypatch.setattr(abuse, "_review", broken)
    await hosting.book(S, 2.5)
    await hosting.check(S)
    [a] = await alerts(l)
    assert a.verdict == "error" and "нет ответа" in a.reason


def test_the_transcript_keeps_the_newest_when_long():
    S = SimpleNamespace(log=[{"type": "user", "text": f"msg{i} " + "x" * 500} for i in range(100)]
                        + [{"type": "tool", "name": "speak"}, {"type": "tutor", "text": "последнее"}])
    t = abuse._transcript(S, limit=3000)
    assert t.endswith("РЕПЕТИТОР: последнее") and "msg99" in t and "msg0 " not in t
