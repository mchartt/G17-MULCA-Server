"""
The embedder "remember" uses for similarity search. The only OpenAI-specific
code for turning text into a vector lives here.
"""

from __future__ import annotations

from openai import AsyncOpenAI

EMBEDDING_MODEL = "text-embedding-3-small"


class OpenAIEmbedder:
    """Satisfies cognition.memory.ports.Embedder against OpenAI's API."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str = EMBEDDING_MODEL,
    ) -> None:
        self._client = client
        self._model = model

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model=self._model,
            input=text,
        )
        return response.data[0].embedding
