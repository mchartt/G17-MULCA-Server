"""
The embedder "remember" uses for similarity search. The only OpenAI-specific
code for turning text into a vector lives here.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from ...usage_report import UsageTracker
from .main_agent import record_cost

EMBEDDING_MODEL = "text-embedding-3-small"


class OpenAIEmbedder:
    """Satisfies cognition.memory.ports.Embedder against OpenAI's API."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str = EMBEDDING_MODEL,
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._usage_tracker = usage_tracker

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model=self._model,
            input=text,
        )
        if getattr(response, "usage", None) is not None:
            record_cost(
                "embedding",
                self._model,
                (response.usage.prompt_tokens, 0, 0, 0, 0, 0),
                tracker=self._usage_tracker,
            )
        return response.data[0].embedding
