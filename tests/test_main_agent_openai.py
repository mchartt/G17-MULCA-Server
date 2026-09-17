import base64

from openai.types.realtime.realtime_error_event import RealtimeErrorEvent
from openai.types.realtime.response_audio_delta_event import ResponseAudioDeltaEvent
from openai.types.realtime.response_done_event import ResponseDoneEvent
from openai.types.realtime.response_text_delta_event import ResponseTextDeltaEvent

from muca_server.agents.main_agent import MainAgentEvent
from muca_server.agents.openai.main_agent import MainAgentOpenAI
from muca_server.agents.options import MainAgentOptions


def make_agent(**kwargs) -> MainAgentOpenAI:
    return MainAgentOpenAI(
        options=MainAgentOptions(),
        client=object(),
        **kwargs,
    )


class FakeItems:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    async def create(self, *, item: dict) -> None:
        self._calls.append(item)


class FakeConversation:
    def __init__(self, calls: list) -> None:
        self.item = FakeItems(calls)


class FakeResponse:
    def __init__(self) -> None:
        self.created = 0

    async def create(self) -> None:
        self.created += 1


class FakeConnection:
    def __init__(self) -> None:
        self.item_calls: list = []
        self.conversation = FakeConversation(self.item_calls)
        self.response = FakeResponse()


def audio_delta(data: bytes) -> ResponseAudioDeltaEvent:
    return ResponseAudioDeltaEvent.model_construct(
        type="response.output_audio.delta",
        delta=base64.b64encode(data).decode("ascii"),
        content_index=0,
        event_id="e1",
        item_id="i1",
        output_index=0,
        response_id="r1",
    )


def text_delta(text: str) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent.model_construct(
        type="response.output_text.delta",
        delta=text,
        content_index=0,
        event_id="e2",
        item_id="i1",
        output_index=0,
        response_id="r1",
    )


def response_done() -> ResponseDoneEvent:
    return ResponseDoneEvent.model_construct(
        type="response.done",
        event_id="e3",
        response=None,
    )


async def test_audio_deltas_reach_the_stream_audio_hook():
    agent = make_agent()
    chunks = []

    async def record(chunk):
        chunks.append(chunk)

    agent.on_audio = record

    await agent._interpret(audio_delta(b"\x00\x00\x80\x3f"))

    assert len(chunks) == 1
    assert chunks[0].data == b"\x00\x00\x80\x3f"


async def test_text_deltas_reach_the_stream_text_hook():
    agent = make_agent()
    deltas = []

    async def record(text, is_final):
        deltas.append((text, is_final))

    agent.on_text = record

    await agent._interpret(text_delta("Hello "))
    await agent._interpret(text_delta("there."))
    await agent._interpret(response_done())

    assert deltas == [
        ("Hello ", False),
        ("there.", False),
        ("", True),
    ]


async def test_speech_lifecycle_events_are_raised():
    agent = make_agent()
    events = []

    async def record(event):
        events.append(event)

    agent.on_event = record

    await agent._interpret(
        _speech_event("input_audio_buffer.speech_started"),
    )
    await agent._interpret(
        _speech_event("input_audio_buffer.speech_stopped"),
    )

    assert events == [
        MainAgentEvent.USER_SPEECH_STARTED,
        MainAgentEvent.USER_SPEECH_STOPPED,
    ]


async def test_a_provider_error_does_not_raise():
    agent = make_agent()

    await agent._interpret(
        RealtimeErrorEvent.model_construct(
            type="error",
            event_id="e4",
            error={"message": "boom", "type": "server_error"},
        )
    )


def _speech_event(event_type: str):
    class _Event:
        type = event_type

    return _Event()


def test_persona_and_task_are_combined_for_the_connect_time_instructions():
    agent = make_agent(persona="You are MULCA.", task_knowledge="Keep the peace.")

    instructions = agent._build_instructions()

    assert "You are MULCA." in instructions
    assert "Keep the peace." in instructions


async def test_trigger_response_pushes_per_turn_content_as_an_item_not_session_instructions():
    agent = make_agent()
    agent._connection = FakeConnection()

    await agent._trigger_response(
        max_tokens=None,
        instructions="Current directive:\nSay hello.",
        add_to_conversation=True,
    )

    assert agent._connection.item_calls == [
        {
            "type": "message",
            "role": "system",
            "content": [
                {"type": "input_text", "text": "Current directive:\nSay hello."},
            ],
        }
    ]
    assert agent._connection.response.created == 1


async def test_trigger_response_pushes_nothing_when_there_is_no_new_content():
    agent = make_agent()
    agent._connection = FakeConnection()

    await agent._trigger_response(
        max_tokens=None,
        instructions="",
        add_to_conversation=True,
    )

    assert agent._connection.item_calls == []
    assert agent._connection.response.created == 1
