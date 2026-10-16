import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.storage import Database
from bot.watcher import SearchWatcher


@pytest.mark.asyncio
async def test_changed_interval_recalculates_current_wait(tmp_path, monkeypatch):
    db = Database(tmp_path / "bot.db")
    await db.set_poll_interval_minutes(15)
    watcher = SearchWatcher(AsyncMock(), database=db, token_manager=AsyncMock())
    delays = []

    async def fake_wait(coroutine, timeout):
        delays.append(timeout)
        if len(delays) == 1:
            await db.set_poll_interval_minutes(5)
            await watcher.reschedule()
            return await coroutine
        coroutine.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait)
    await watcher._wait_for_next_cycle()
    assert delays[0] == pytest.approx(900, abs=2)
    assert delays[1] == pytest.approx(300, abs=2)
    assert not watcher._reschedule.is_set()
