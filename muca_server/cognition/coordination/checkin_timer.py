"""
CheckinTimer: the check-in on time (spec FR15), not only on command.

Every POLL_SECONDS, while the group's session is open, it asks: has a full
interval passed since the last timed check-in? If so it runs the normal
check-in (plan_commands.PlanCommands.timed_checkin), which posts a card only
when checkin.findings() sees something late or at risk, and stays silent
otherwise. The time of the last timed check-in is saved in the group's
SQLite file (store mark "checkin"), so a restart neither skips nor repeats it.

Off unless MUCA_COORDINATOR_CHECKIN_HOURS is set (settings.py). The first
time a group is seen, the clock starts: no check-in at the very first open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .store import CoordinationStore

logger = logging.getLogger(__name__)

POLL_SECONDS = 10 * 60  # how often the timer looks (cheap: one SQLite read)
FIRST_LOOK_SECONDS = 60  # after the session opens, let the members arrive first
MARK = "checkin"


class CheckinTimer:
    def __init__(
        self,
        conversation: Any,
        *,
        interval_seconds: float,
        store: Callable[[], CoordinationStore],
        run_checkin: Callable[[], Awaitable[bool]],
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._conversation = conversation
        self._interval = interval_seconds
        self._store = store
        self._run_checkin = run_checkin
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start looking (nothing happens when the interval is 0 = off)."""
        if self._interval > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        await self._sleep(FIRST_LOOK_SECONDS)
        while True:
            try:
                await self.tick()
            except Exception:
                # Project rule: a coordinator error never breaks the chat.
                logger.exception("timed check-in failed")
            await self._sleep(POLL_SECONDS)

    async def tick(self) -> bool:
        """One look. True when a check-in ran and posted a card."""
        conversation = self._conversation
        if conversation.is_private or conversation.is_ephemeral or not conversation.conversation_id:
            return False
        store = self._store()
        now = self._clock()
        mark = await store.mark(MARK)
        if mark is None:
            await store.save_mark(MARK, 0, now)  # first time: the interval starts now
            return False
        if now - mark[1] < self._interval:
            return False
        # Saved before running: a failing model is not retried every 10 minutes.
        await store.save_mark(MARK, 0, now)
        return await self._run_checkin()
