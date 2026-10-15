from types import SimpleNamespace

from muca_server.agents.openai.web_search import (
    WEB_SEARCH_CALL_FEE_USD,
    OpenAIWebSearcher,
)
from muca_server.usage_report import usage_sink

from test_usage_events import FakeTracker


def fake_usage(input_tokens=100, cached_tokens=20, output_tokens=50):
    return SimpleNamespace(
        input_tokens=input_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        output_tokens=output_tokens,
    )


class FakeResponses:
    def __init__(
        self,
        *,
        output_text: str = "",
        raises: bool = False,
        usage=None,
    ) -> None:
        self._output_text = output_text
        self._raises = raises
        self._usage = usage
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("boom")
        return SimpleNamespace(output_text=self._output_text, usage=self._usage)


class FakeClient:
    def __init__(
        self,
        *,
        output_text: str = "",
        raises: bool = False,
        usage=None,
    ) -> None:
        self.responses = FakeResponses(
            output_text=output_text, raises=raises, usage=usage,
        )


async def test_search_uses_the_hosted_web_search_tool():
    client = FakeClient(output_text="Rome is warm in October, highs near 22C.")
    searcher = OpenAIWebSearcher(client)

    result = await searcher.search("weather in Rome today")

    assert result == "Rome is warm in October, highs near 22C."
    call = client.responses.calls[0]
    assert call["tools"] == [{"type": "web_search"}]
    assert call["input"] == "weather in Rome today"


async def test_an_empty_result_is_reported_as_no_results():
    client = FakeClient(output_text="")
    searcher = OpenAIWebSearcher(client)

    assert await searcher.search("something obscure") == "(no results found)"


async def test_a_failed_search_does_not_raise():
    client = FakeClient(raises=True)
    searcher = OpenAIWebSearcher(client)

    result = await searcher.search("weather in Rome today")

    assert "failed" in result.lower()


async def test_a_successful_search_records_its_cost_plus_the_flat_tool_fee():
    usage_sink.set(None)
    tracker = FakeTracker()
    client = FakeClient(
        output_text="Rome is warm in October.",
        usage=fake_usage(input_tokens=100, cached_tokens=20, output_tokens=50),
    )
    searcher = OpenAIWebSearcher(client, usage_tracker=tracker)

    await searcher.search("weather in Rome today")

    assert len(tracker.recorded) == 1
    kind, instance, tokens, cost = tracker.recorded[0]
    assert kind == "web_search"
    assert tokens == (80, 20, 0, 0, 50, 0)  # fresh text, cached text, ..., out text
    # Token cost (gpt-4o-mini: 0.15/0.075/0.60 per million) plus the flat
    # per-call tool fee, not token-priced at all.
    token_cost = (80 * 0.15 + 20 * 0.075 + 50 * 0.60) / 1_000_000
    assert cost == token_cost + WEB_SEARCH_CALL_FEE_USD


async def test_a_failed_search_records_no_cost():
    usage_sink.set(None)
    tracker = FakeTracker()
    client = FakeClient(raises=True)
    searcher = OpenAIWebSearcher(client, usage_tracker=tracker)

    await searcher.search("weather in Rome today")

    assert tracker.recorded == []


async def test_cited_pages_are_listed_as_sources_once_each():
    def cite(title, url):
        return SimpleNamespace(type="url_citation", title=title, url=url)

    response = SimpleNamespace(
        output_text="Warming is unequivocal.",
        usage=None,
        output=[
            SimpleNamespace(type="web_search_call"),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        annotations=[
                            cite("AR6 Synthesis Report", "https://www.ipcc.ch/ar6"),
                            cite("AR6 Synthesis Report", "https://www.ipcc.ch/ar6"),
                            cite("State of the Global Climate", "https://wmo.int/s"),
                        ]
                    )
                ],
            ),
        ],
    )
    client = FakeClient()

    async def create(**kwargs):
        return response

    client.responses.create = create

    result = await OpenAIWebSearcher(client).search("is climate change real")

    assert result == (
        "Warming is unequivocal.\n\nSources:\n"
        "- AR6 Synthesis Report: https://www.ipcc.ch/ar6\n"
        "- State of the Global Climate: https://wmo.int/s"
    )
