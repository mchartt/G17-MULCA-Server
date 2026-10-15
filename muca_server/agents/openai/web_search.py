"""
Looking something up outside the conversation, on OpenAI.

Satisfies cognition.ports.WebSearcher structurally. Uses the Responses API's
hosted web_search tool, not a separate search vendor -- same client, same
API key as every other OpenAI-backed piece here, nothing new to provision.

Used identically for text and voice turns -- CoalaPolicy.on_intervention_
signal is what differs per modality: text calls this once, inline, before
building its one reply; voice says a quick "let me check that" first (a
separate spoken turn), then calls this, then reports the findings as a
second spoken turn, rather than searching silently inside one reply with
an unexplained multi-second gap in it.
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

from ...usage_report import UsageTracker
from .main_agent import record_cost

logger = logging.getLogger(__name__)

DEFAULT_WEB_SEARCH_MODEL = "gpt-4o-mini"

# The web_search tool's own per-call fee, separate from (and on top of)
# whatever its result content costs in ordinary model tokens -- not
# token-priced at all. Community-reported figures for this specific tool
# range $10-25 per 1,000 calls depending on model and vary between OpenAI's
# own pricing page and docs by account (checked 2026-10-08; no single
# authoritative figure found). Picked the lower, more conservative end
# ($10/1000) rather than overstate cost -- treat this as an approximation
# to check against the real invoice, not a confirmed rate.
WEB_SEARCH_CALL_FEE_USD = 0.01


class OpenAIWebSearcher:
    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str = DEFAULT_WEB_SEARCH_MODEL,
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._usage_tracker = usage_tracker

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

        usage = getattr(response, "usage", None)
        if usage is not None:
            details = usage.input_tokens_details
            record_cost(
                "web_search",
                self._model,
                (
                    usage.input_tokens - details.cached_tokens,
                    details.cached_tokens,
                    0,
                    0,
                    usage.output_tokens,
                    0,
                ),
                tracker=self._usage_tracker,
                extra_fee_usd=WEB_SEARCH_CALL_FEE_USD,
            )

        result = response.output_text or "(no results found)"
        sources = _sources(response)
        if sources:
            result += "\n\nSources:\n" + "\n".join(
                f"- {title}: {url}" for title, url in sources
            )
        logger.info(
            "Web search for %r returned %d chars",
            query,
            len(result),
        )
        return result


def _sources(response) -> list[tuple[str, str]]:
    """
    The pages the search cited, each once, in order. Listed separately:
    found live (15 Oct), the answer named its sources but never linked
    them -- the summary text doesn't reliably carry the URLs.
    """

    seen: dict[str, str] = {}
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            for annotation in getattr(content, "annotations", None) or []:
                if getattr(annotation, "type", None) != "url_citation":
                    continue
                url = getattr(annotation, "url", "")
                if url and url not in seen:
                    seen[url] = getattr(annotation, "title", "") or url
    return [(title, url) for url, title in seen.items()]
