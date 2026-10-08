"""
Looking something up outside the conversation, on OpenAI.

Satisfies cognition.ports.WebSearcher structurally. Uses the Responses API's
hosted web_search tool, not a separate search vendor -- same client, same
API key as every other OpenAI-backed piece here, nothing new to provision.

NOT YET WIRED IN for voice: CoalaPolicy only calls this for a text-modality
utterance (see its own comment). A voice reply asking "let me look that up"
before the real answer, then speaking it once this resolves, is a two-stage
conversational flow this first cut deliberately doesn't attempt -- voice
still just forces a normal reply instead, exactly as if needs_web_search had
never been set, until that's built.
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

DEFAULT_WEB_SEARCH_MODEL = "gpt-4o-mini"


class OpenAIWebSearcher:
    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str = DEFAULT_WEB_SEARCH_MODEL,
    ) -> None:
        self._client = client
        self._model = model

    async def search(self, query: str) -> str:
        try:
            response = await self._client.responses.create(
                model=self._model,
                tools=[{"type": "web_search"}],
                input=query,
            )
        except Exception:
            logger.exception("Web search failed for %r", query)
            return "(web search failed; answer from what you already know)"

        return response.output_text or "(no results found)"
