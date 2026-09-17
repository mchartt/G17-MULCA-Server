import json
from types import SimpleNamespace

from muca_server.agents.openai.main_agent import ChatCompletionsSummarizer


class FakeChatCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content: str) -> None:
        self.chat = SimpleNamespace(
            completions=FakeChatCompletions(content),
        )


async def test_group_summary_uses_a_json_schema_response():
    content = json.dumps(
        {
            "history_summary": "they discussed the weather",
            "participant_roles_summary": "Ada leads",
        }
    )
    client = FakeClient(content)
    summarizer = ChatCompletionsSummarizer(client)

    history, roles = await summarizer.summarize(
        transcript="Ada: hi\nGrace: hello",
        previous_summary="",
        previous_roles="",
    )

    assert history == "they discussed the weather"
    assert roles == "Ada leads"

    call = client.chat.completions.calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["model"] == "gpt-4o"
