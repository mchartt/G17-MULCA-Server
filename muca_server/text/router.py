import json
from typing import Any
from uuid import uuid4

from ..sessions.session_state import SessionState
from ..protocol import TextMessage

class TextRouter:
    def __init__(
        self,
        *,
        session_state: SessionState,
    ) -> None:
        self._session_state = session_state

    async def route(self, message: TextMessage) -> None:
        """Parse one text message and route supported campaign-control payloads."""

        try:
            payload: Any = json.loads(message.text)
        except json.JSONDecodeError:
            await self._route_plain_text(message.text)
            return

        if not isinstance(payload, dict):
            await self._route_plain_text(message.text)
            return

        await self._route_json(payload)

    async def _route_plain_text(self, payload: str) -> None:
        # Plain conversational text will be routed to agents in a later module.
        return None

    async def _route_json(self, payload: dict[str, Any]) -> None:
        match payload.get("Type"):
            case _:
                raise ValueError("Unknown message type.")