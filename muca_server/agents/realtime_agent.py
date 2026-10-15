from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..audio import AudioChunk

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str], Awaitable[None]]
ConnectionLostHandler = Callable[["RealtimeAgent"], Awaitable[None]]


class RealtimeAgent:
    """
    Talks to one realtime provider on behalf of a conversation.

    The base implementation reaches no provider: every call below is a
    logged no-op. A provider-specific subclass (an OpenAI or Gemini agent)
    overrides the hooks at the bottom of this class to actually open a
    connection and speak that provider's dialect, while the public methods
    above stay the same for every provider.
    """

    def __init__(self) -> None:
        self._connected = False

        # Whether the provider currently has an unfinished response; a
        # provider subclass flips this from its own _trigger_response/
        # _interpret. Tracked here (not just left to interrupt_response) so
        # a redundant cancel is never sent -- OpenAI's Realtime API errors on
        # cancelling when nothing is active, it does not treat it as a no-op.
        self._response_active = False

        # For the logs: which conversation this agent serves, and what its
        # responses have cost so far (estimated, in USD).
        self.log_label = ""
        self.cost_usd = 0.0

        # Which bot/team to attribute usage_report.py's cost to; falls back
        # to log_label (the conversation_id) if a client never set one.
        self.instance_label = ""

        # Raw provider messages, exactly as they arrive.
        self.on_message: MessageHandler | None = None

        # The provider ended the connection on its own -- OpenAI closes
        # every Realtime session after 60 minutes, for one. Never called for
        # a disconnect() asked for here.
        self.on_connection_lost: ConnectionLostHandler | None = None

        # The live session reached its token limit (once, until the next
        # clear_context): the conversation renews its memory instead of the
        # oldest half being dropped with nothing in its place.
        self.on_context_full: Callable[[], Awaitable[None]] | None = None
        self._context_full_reported = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._connected:
            return

        await self._connect()
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return

        self._connected = False
        await self._disconnect()

    async def send_anything(self, json_payload: str) -> None:
        await self._send_anything(json_payload)

    async def update_session(self, session: dict[str, Any]) -> None:
        """A client's session.update; each provider applies what it can."""

        await self._update_session(session)

    async def send_text(
        self,
        text: str,
        *,
        role: str = "system",
    ) -> None:
        await self._send_text(text, role=role)

    async def send_audio_chunk(self, chunk: AudioChunk) -> None:
        # Audio streams continuously, including while this agent is still
        # being connected.
        if not self._connected:
            return

        await self._send_audio_chunk(chunk)

    async def trigger_response(
        self,
        *,
        max_tokens: int | None = None,
        interrupt: bool = True,
        instructions: str = "",
        add_to_conversation: bool = True,
        output_modality: str | None = None,
    ) -> None:
        """
        Ask the provider for a response.

        `output_modality` is "text" or "audio" for this one response; None
        keeps the agent's session default.
        """

        if interrupt:
            await self.interrupt_response()

        await self._trigger_response(
            max_tokens=max_tokens,
            instructions=instructions,
            add_to_conversation=add_to_conversation,
            output_modality=output_modality,
        )

    async def interrupt_response(self) -> None:
        if not self._response_active:
            # Nothing to cancel: skip the call rather than sending it anyway,
            # since trigger_response's interrupt=True default would otherwise
            # send a cancel on every ordinary turn, not just a real interrupt.
            return

        await self._cancel_response()

    async def _handle_provider_message(self, raw: str) -> None:
        """A provider subclass calls this as messages arrive."""

        if self.on_message is not None:
            await self.on_message(raw)

    async def _connection_lost(self, reason: str) -> None:
        """
        A provider subclass calls this when its connection ends without a
        disconnect() -- after which every send would only fail, so stop
        claiming to be connected and let the owner decide what to do.
        """

        if not self._connected:
            # disconnect() already cleared it: this end was asked for.
            return

        self._connected = False
        self._response_active = False
        logger.warning(
            "%s lost its provider connection: %s",
            type(self).__name__,
            reason,
        )

        if self.on_connection_lost is not None:
            await self.on_connection_lost(self)

    async def clear_context(self) -> None:
        """Start the live session over: everything in it so far is dropped."""

        self._context_full_reported = False
        await self._clear_context()

    # ------------------------------------------------------------------
    # Provider hooks — override in a provider-specific subclass.
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        logger.info("No provider configured, connect() is a no-op")

    async def _disconnect(self) -> None:
        pass

    async def _clear_context(self) -> None:
        pass

    async def _send_anything(self, json_payload: str) -> None:
        logger.debug("No provider configured, dropping payload")

    async def _update_session(self, session: dict[str, Any]) -> None:
        logger.debug("No provider configured, dropping session update")

    async def _send_text(self, text: str, *, role: str) -> None:
        logger.debug(
            "No provider configured, dropping %s text: %s",
            role,
            text,
        )

    async def _send_audio_chunk(self, chunk: AudioChunk) -> None:
        logger.debug(
            "No provider configured, dropping %d audio samples",
            chunk.sample_count,
        )

    async def _trigger_response(
        self,
        *,
        max_tokens: int | None,
        instructions: str,
        add_to_conversation: bool,
        output_modality: str | None = None,
    ) -> None:
        logger.debug("No provider configured, dropping response trigger")

    async def _cancel_response(self) -> None:
        logger.debug("No provider configured, dropping response cancel")
