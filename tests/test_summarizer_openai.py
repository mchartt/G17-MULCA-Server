import json
from types import SimpleNamespace

import pytest

from muca_server.agents.openai.day_summarizer import OpenAIDaySummarizer

DAY = {
    "narrative": "Ada and Grace picked the weather app as their project.",
    "decisions": [
        {"decision": "build a weather app", "decided_by": ["Ada", "Grace"], "rationale": "both use one daily"}
    ],
    "action_points": [{"owner": "Grace", "task": "send the forecast API link", "status": "open"}],
    "conclusions": [],
    "knowledge": [],
    "contributions": [{"name": "Ada", "items": ["proposed the weather app"]}],
    "decision_making": [],
    "process_notes": [],
    "mulca": [],
}


class FakeChatCompletions:
    def __init__(self, content: str, *, failing_models=()) -> None:
        self._content = content
        self._failing = set(failing_models)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["model"] in self._failing:
            raise RuntimeError(f"no access to {kwargs['model']}")
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content: str, **kwargs) -> None:
        self.chat = SimpleNamespace(
            completions=FakeChatCompletions(content, **kwargs),
        )


async def test_a_day_is_summarized_from_its_log_with_a_strict_schema():
    client = FakeClient(json.dumps(DAY))
    summarizer = OpenAIDaySummarizer(client)

    summary = await summarizer.summarize_day(
        day="2026-10-14",
        log="Ada (text): let's build a weather app",
        final=True,
    )

    assert summary == DAY
    call = client.chat.completions.calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    user = call["messages"][1]["content"]
    assert "Wednesday 14 October 2026" in user
    assert "Ada (text): let's build a weather app" in user
    # No word limit anywhere: length follows content.
    assert "words" not in call["messages"][0]["content"]


async def test_the_once_a_day_summaries_use_the_stronger_model():
    client = FakeClient(json.dumps(DAY))
    summarizer = OpenAIDaySummarizer(client, daily_model="strong", model="usual")

    await summarizer.summarize_day(day="2026-10-14", log="x", final=True)
    await summarizer.summarize_day(day="2026-10-15", log="x", final=False)
    await summarizer.build_overview(days=[("2026-10-14", DAY)])

    assert [c["model"] for c in client.chat.completions.calls] == [
        "strong",  # a finished day
        "usual",  # today so far, replaced anyway
        "strong",  # the overview
    ]


async def test_the_overview_is_built_from_the_day_summaries_with_their_dates():
    client = FakeClient(json.dumps(DAY))

    await OpenAIDaySummarizer(client).build_overview(
        days=[("2026-10-14", DAY), ("2026-10-15", DAY)],
    )

    call = client.chat.completions.calls[0]
    user = call["messages"][1]["content"]
    assert "Wednesday 14 October 2026" in user
    assert "Thursday 15 October 2026" in user
    assert "build a weather app" in user
    properties = call["response_format"]["json_schema"]["schema"]["properties"]
    decision = properties["decisions"]["items"]["properties"]
    assert {"date", "changed_on"} <= set(decision)
    assert "raised_on" in properties["action_points"]["items"]["properties"]


async def test_no_access_to_the_stronger_model_falls_back_to_the_usual_one():
    client = FakeClient(json.dumps(DAY), failing_models={"strong"})
    summarizer = OpenAIDaySummarizer(client, daily_model="strong", model="usual")

    summary = await summarizer.summarize_day(day="2026-10-14", log="x", final=True)

    assert summary == DAY
    assert [c["model"] for c in client.chat.completions.calls] == ["strong", "usual"]


async def test_if_every_model_fails_the_error_reaches_the_caller():
    client = FakeClient(json.dumps(DAY), failing_models={"strong", "usual"})
    summarizer = OpenAIDaySummarizer(client, daily_model="strong", model="usual")

    with pytest.raises(RuntimeError):
        await summarizer.summarize_day(day="2026-10-14", log="x", final=True)
