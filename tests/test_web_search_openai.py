from types import SimpleNamespace

from muca_server.agents.openai.web_search import OpenAIWebSearcher


class FakeResponses:
    def __init__(self, *, output_text: str = "", raises: bool = False) -> None:
        self._output_text = output_text
        self._raises = raises
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("boom")
        return SimpleNamespace(output_text=self._output_text)


class FakeClient:
    def __init__(self, *, output_text: str = "", raises: bool = False) -> None:
        self.responses = FakeResponses(output_text=output_text, raises=raises)


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
