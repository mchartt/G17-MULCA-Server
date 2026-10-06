"""
The realtime models the server uses, which an admin can change at runtime
(the `set_model` message) and which survive a restart.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import suppress
from pathlib import Path
from weakref import WeakSet

from ...protocol import EndSession, SetModelMessage
from .intervention_agent import DEFAULT_MODEL as DEFAULT_INTERVENTION_MODEL
from .main_agent import DEFAULT_REALTIME_MODEL

logger = logging.getLogger(__name__)


class ModelSettings:
    def __init__(self, path: Path, admin_token: str | None = None) -> None:
        self.main = DEFAULT_REALTIME_MODEL
        self.intervention = DEFAULT_INTERVENTION_MODEL
        self._path = path
        self._admin_token = admin_token
        # Every live session's outbound queue, so a change can end them all.
        self._queues: WeakSet[asyncio.Queue] = WeakSet()

        if path.exists():
            saved = json.loads(path.read_text())
            self.main = saved.get("main", self.main)
            self.intervention = saved.get("intervention", self.intervention)

        logger.info(
            "Realtime models: main=%s, intervention=%s (from %s)",
            self.main,
            self.intervention,
            f"saved settings in {path}" if path.exists() else "defaults",
        )

    def watch(self, outbound_queue: asyncio.Queue) -> None:
        self._queues.add(outbound_queue)

    def apply(self, message: SetModelMessage) -> None:
        if not self._admin_token or not hmac.compare_digest(
            message.admin_token.encode(),
            self._admin_token.encode(),
        ):
            raise PermissionError("Invalid admin token")

        self.main = message.model
        if message.include_intervention:
            self.intervention = message.model

        self._path.write_text(
            json.dumps({"main": self.main, "intervention": self.intervention}),
        )
        logger.info(
            "Realtime models changed: main=%s, intervention=%s",
            self.main,
            self.intervention,
        )

        # Clients reopen on 1012, building new agents with the new model.
        for queue in self._queues:
            with suppress(asyncio.QueueFull):
                queue.put_nowait(EndSession(reason="Model changed"))
