"""
The cost estimate reaches the session's client as `usage` events: from both
Realtime agents, the main agent's transcription, the summarizer and the
embedder -- the last two shared by every session, attributed through
usage_report.usage_sink.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from muca_server.agents.openai.day_summarizer import OpenAIDaySummarizer
from muca_server.agents.openai.embeddings import OpenAIEmbedder
from muca_server.agents.openai.main_agent import (
    MainAgentOpenAI,
    record_cost,
)
from muca_server.agents.options import MainAgentOptions
from muca_server.protocol import UsageMessage, serialize_outgoing_message
from muca_server.usage_report import usage_sink

from test_main_agent_openai import FakeConnection, _Event, _Obj


class FakeTracker:
    def __init__(self) -> None:
        self.recorded: list[tuple] = []

    def record_usage(self, kind, instance, tokens, cost) -> None:
        self.recorded.append((kind, instance, tokens, cost))

    async def maybe_alert(self) -> None:
        pass


def capture_usage(label: str = "MULCA-group3") -> list[tuple]:
    sent: list[tuple] = []

    async def send(kind, tokens, cost) -> None:
        sent.append((kind, tokens, cost))

    usage_sink.set((label, send))
    return sent


async def test_a_call_is_priced_reported_and_sent_to_the_session():
    sent = capture_usage()
    tracker = FakeTracker()

    cost = record_cost(
        "summary",
        "gpt-4o",
        (1_000_000, 0, 0, 0, 100_000, 0),
        tracker=tracker,
    )
    await asyncio.sleep(0)

    assert cost == pytest.approx(2.5 + 1.0)
    assert tracker.recorded == [
        ("summary", "MULCA-group3", (1_000_000, 0, 0, 0, 100_000, 0), cost),
    ]
    assert sent == [("summary", (1_000_000, 0, 0, 0, 100_000, 0), cost)]


async def test_extra_fee_usd_adds_a_flat_amount_on_top_of_token_cost():
    # The web search tool's own per-call fee, separate from (and on top
    # of) whatever its result content costs in ordinary model tokens.
    usage_sink.set(None)
    tracker = FakeTracker()

    cost = record_cost(
        "web_search",
        "gpt-4o-mini",
        (0, 0, 0, 0, 0, 0),
        tracker=tracker,
        extra_fee_usd=0.01,
    )

    assert cost == pytest.approx(0.01)
    assert tracker.recorded == [("web_search", "", (0, 0, 0, 0, 0, 0), cost)]


async def test_without_a_session_nothing_is_sent():
    usage_sink.set(None)

    assert record_cost("embedding", "text-embedding-3-small", (10, 0, 0, 0, 0, 0)) > 0


async def test_a_transcription_is_priced_as_transcription():
    sent = capture_usage()
    agent = MainAgentOpenAI(options=MainAgentOptions(), client=object())

    await agent._interpret(
        _Event(
            "conversation.item.input_audio_transcription.completed",
            item_id="i1",
            transcript="ciao",
            usage=_Obj(
                type="tokens",
                input_token_details=_Obj(text_tokens=20, audio_tokens=1_000),
                output_tokens=5,
            ),
        )
    )
    await asyncio.sleep(0)

    [(kind, tokens, cost)] = sent
    assert (kind, tokens) == ("transcription", (20, 0, 1_000, 0, 5, 0))
    assert cost == pytest.approx((20 + 1_000) * 2.5e-6 + 5 * 10e-6)


async def test_the_summarizer_and_the_embedder_report_their_calls():
    sent = capture_usage()
    usage = SimpleNamespace(
        prompt_tokens=1_000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=400),
    )
    content = json.dumps({"narrative": "n"})

    async def chat_create(**kwargs):
        message = SimpleNamespace(content=content)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=usage,
        )

    async def embed_create(**kwargs):
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1])],
            usage=SimpleNamespace(prompt_tokens=12),
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
        embeddings=SimpleNamespace(create=embed_create),
    )

    await OpenAIDaySummarizer(client).summarize_day(
        day="2026-10-14",
        log="Ada (text): hi",
        final=False,
    )
    await OpenAIEmbedder(client).embed("hi")
    await asyncio.sleep(0)

    assert [(kind, tokens) for kind, tokens, _ in sent] == [
        ("summary", (600, 400, 0, 0, 200, 0)),
        ("embedding", (12, 0, 0, 0, 0, 0)),
    ]


def test_a_usage_event_serializes_in_the_protocols_camel_case():
    frame = json.loads(
        serialize_outgoing_message(
            UsageMessage(
                agent="main",
                fresh_text=1,
                cached_text=2,
                fresh_audio=3,
                cached_audio=4,
                out_text=5,
                out_audio=6,
                cost_usd=0.25,
            )
        )
    )

    assert frame == {
        "event": "usage",
        "agent": "main",
        "freshText": 1,
        "cachedText": 2,
        "freshAudio": 3,
        "cachedAudio": 4,
        "outText": 5,
        "outAudio": 6,
        "costUsd": 0.25,
    }


async def test_a_client_can_switch_every_reply_to_text_and_back():
    agent = MainAgentOpenAI(options=MainAgentOptions(), client=object())
    agent._connection = FakeConnection()

    async def reply_to_a_spoken_turn() -> list[str]:
        await agent._trigger_response(
            max_tokens=None,
            instructions="",
            add_to_conversation=True,
            output_modality="audio",
        )
        return agent._connection.response.params[-1]["output_modalities"]

    await agent.update_session({"output_modalities": ["text"]})
    assert await reply_to_a_spoken_turn() == ["text"]

    await agent.update_session({"output_modalities": ["audio"]})
    assert await reply_to_a_spoken_turn() == ["audio"]

    # Only a modality change: the provider's session is left alone.
    assert agent._connection.session.updates == []
